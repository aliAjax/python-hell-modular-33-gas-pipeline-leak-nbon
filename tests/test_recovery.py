import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_state_versions_and_audit_survive_restart(self):
        repo = Repository(self.tmp.name)
        repo.initialize()
        service = Service(repo)
        item = service.create_item({
            "pipeline_id": "P-5",
            "segment_id": "S-1",
            "reported_at": "2026-09-27T12:00:00+00:00",
            "pressure_drop_kpa": 55,
            "sensor_value_ppm": 300,
            "odor_reports": 4,
            "reporter": "dispatch-5",
        }, "d", "dispatcher")
        item = service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"])
        item = service.act(item["id"], "valve_receipt", {"receipts": [
            {"receipt_id": "R-1", "valve_id": "V-1", "result": "closed"}
        ]}, "s", "supervisor", item["version"])
        item_id = item["id"]
        version_before_restart = item["version"]
        self.assertEqual(item["status"], "isolating")
        self.assertEqual(item["checkpoint"]["pending"], ["V-2"])

        # 模拟服务重启：新建仓储/服务实例，状态、版本、检查点都应接得上
        repo2 = Repository(self.tmp.name)
        service2 = Service(repo2)
        resumed = service2.get_item(item_id)
        self.assertEqual(resumed["status"], "isolating")
        self.assertEqual(resumed["version"], version_before_restart)
        self.assertEqual(resumed["checkpoint"]["pending"], ["V-2"])
        self.assertTrue(resumed["audit_chain"]["valid"])

        # 从断点继续：补 V-2 回执，流程继续
        resumed = service2.act(resumed["id"], "valve_receipt", {"receipts": [
            {"receipt_id": "R-2", "valve_id": "V-2", "result": "closed"}
        ]}, "s", "supervisor", resumed["version"])
        self.assertEqual(resumed["status"], "isolated")
        # 重复回执在重启后同样不重复执行
        again = service2.act(resumed["id"], "valve_receipt", {"receipts": [
            {"receipt_id": "R-2", "valve_id": "V-2", "result": "closed"}
        ]}, "s", "supervisor", resumed["version"])
        self.assertEqual(again["version"], resumed["version"])
        self.assertTrue(again["audit_chain"]["valid"])

    def test_audit_chain_detects_tampering(self):
        repo = Repository(self.tmp.name)
        repo.initialize()
        service = Service(repo)
        item = service.create_item({
            "pipeline_id": "P-6",
            "segment_id": "S-3",
            "reported_at": "2026-09-27T13:00:00+00:00",
            "pressure_drop_kpa": 10,
            "sensor_value_ppm": 20,
            "odor_reports": 0,
            "reporter": "dispatch-6",
        }, "d", "dispatcher")
        self.assertTrue(service.get_item(item["id"])["audit_chain"]["valid"])

        # 篡改审计载荷：链路校验必须断裂
        conn = repo.connect()
        try:
            event = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id LIMIT 1", (item["id"],)).fetchone()
            payload = json.loads(event["payload"])
            payload["tampered"] = True
            conn.execute("UPDATE audit_events SET payload=? WHERE id=?", (json.dumps(payload, sort_keys=True), event["id"]))
        finally:
            conn.close()
        result = repo.verify_chain(item["id"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["broken_at"], event["id"])


if __name__ == "__main__":
    unittest.main()
