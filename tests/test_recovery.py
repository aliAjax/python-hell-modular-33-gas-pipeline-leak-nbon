import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create_verified_isolating(self):
        item, _ = self.service.create_item({
            "pipeline_id": "P-3",
            "segment_id": "S-2",
            "reported_at": "2026-09-28T07:00:00+00:00",
            "pressure_drop_kpa": 40,
            "sensor_value_ppm": 200,
            "odor_reports": 2,
            "reporter": "patrol-1",
        }, "patrol-1", "patrol")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"])
        return item

    def test_isolation_retry_from_breakpoint_and_duplicate_receipt(self):
        item = self._create_verified_isolating()
        item = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-1", "command_id": "cmd-1", "result": "closed"}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-2", "command_id": "cmd-2", "result": "failed"}, "r", "responder", item["version"])
        self.assertEqual(item["status"], "isolating")
        self.assertEqual(item["payload"]["isolation"]["commands"]["V-2"]["state"], "failed")
        # 重复回执不重复执行：状态和版本都不变
        duplicate = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-2", "command_id": "cmd-2", "result": "failed"}, "r", "responder", item["version"])
        self.assertEqual(duplicate["version"], item["version"])
        self.assertEqual(len(duplicate["payload"]["isolation"]["commands"]["V-2"]["attempts"]), 1)
        # 断点重试：只补发失败的阀门，已关闭的阀门保持原样
        item = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-2", "command_id": "cmd-3", "result": "closed"}, "r", "responder", item["version"])
        self.assertEqual(item["status"], "isolated")
        commands = item["payload"]["isolation"]["commands"]
        self.assertEqual(commands["V-1"]["state"], "closed")
        self.assertEqual(len(commands["V-1"]["attempts"]), 1)
        self.assertEqual(len(commands["V-2"]["attempts"]), 2)

    def test_valve_conflict_requires_field_confirmation(self):
        item = self._create_verified_isolating()
        item = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-9", "command_id": "cmd-x", "result": "closed"}, "r", "responder", item["version"])
        self.assertTrue(item["payload"]["valve_status_conflict"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        self.assertEqual(context.exception.code, "valve_status_conflict")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True, "valve_status_confirmed": True}, "r", "responder", item["version"])
        self.assertFalse(item["payload"]["valve_status_conflict"])
        revisions = item["payload"]["verifications"]
        self.assertEqual(len(revisions), 2)
        self.assertTrue(revisions[-1]["valve_status_resolved"])
        self.assertEqual(revisions[0]["item_version"], 1)

    def test_reconciliation_mismatch_goes_pending_then_restores(self):
        item = self._create_verified_isolating()
        item = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-1", "command_id": "cmd-1", "result": "closed"}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-2", "command_id": "cmd-2", "result": "closed"}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "repair", {"work_order": "WO-9"}, "t", "technician", item["version"])
        item = self.service.act(item["id"], "pressure_test", {"test_passed": True, "pressure_kpa": 150, "minimum_pressure_kpa": 100}, "t", "technician", item["version"])
        # 缺少危险解除记录，对账不上，留在待处理
        item = self.service.act(item["id"], "restore", {}, "s", "supervisor", item["version"])
        self.assertEqual(item["status"], "pending")
        mismatches = item["payload"]["reconciliation"]["mismatches"]
        self.assertEqual([m["type"] for m in mismatches], ["hazard_clearance"])
        # 补齐危险解除记录后重新对账，恢复供气
        item = self.service.act(item["id"], "clear_hazard", {"note": "检测合格"}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "restore", {}, "s", "supervisor", item["version"])
        self.assertEqual(item["status"], "restored")
        self.assertTrue(item["payload"]["reconciliation"]["matched"])

    def test_restart_preserves_state_version_and_audit_chain(self):
        item = self._create_verified_isolating()
        item = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-1", "command_id": "cmd-1", "result": "closed"}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "valve_receipt", {"valve_id": "V-2", "command_id": "cmd-2", "result": "failed"}, "r", "responder", item["version"])
        version_before = item["version"]
        # 模拟服务重启：同一数据库文件重新初始化
        restarted = Service(Repository(self.tmp.name))
        restarted.repository.initialize()
        reloaded = restarted.get_item(item["id"])
        self.assertEqual(reloaded["status"], "isolating")
        self.assertEqual(reloaded["version"], version_before)
        self.assertTrue(restarted.verify_audit(item["id"])["chain_valid"])
        # 重启后从断点继续：重试失败的阀门
        reloaded = restarted.act(reloaded["id"], "valve_receipt", {"valve_id": "V-2", "command_id": "cmd-3", "result": "closed"}, "r", "responder", reloaded["version"])
        self.assertEqual(reloaded["status"], "isolated")
        self.assertEqual(reloaded["version"], version_before + 1)
        self.assertTrue(restarted.verify_audit(item["id"])["chain_valid"])

    def test_audit_chain_detects_tampering(self):
        item = self._create_verified_isolating()
        conn = self.repo.connect()
        try:
            conn.execute("UPDATE audit_events SET payload='{}' WHERE item_id=?", (item["id"],))
        finally:
            conn.close()
        self.assertFalse(self.service.verify_audit(item["id"])["chain_valid"])


if __name__ == "__main__":
    unittest.main()
