import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError, ConflictError


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "pipeline_id": "P-3",
            "segment_id": "S-6",
            "reported_at": "2026-09-27T10:00:00+00:00",
            "pressure_drop_kpa": 40,
            "sensor_value_ppm": 160,
            "odor_reports": 2,
            "reporter": "dispatch-3",
        }, "d", "dispatcher")
        self.item = self.service.act(self.item["id"], "verify", {"field_confirmed": True}, "r", "responder", self.item["version"])
        self.item = self.service.act(self.item["id"], "isolate", {"valve_sequence": ["V-1", "V-2", "V-3"]}, "s", "supervisor", self.item["version"])

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _receipt(self, item, receipt_id, valve_id, result="closed"):
        return self.service.act(item["id"], "valve_receipt", {"receipts": [
            {"receipt_id": receipt_id, "valve_id": valve_id, "result": result}
        ]}, "s", "supervisor", item["version"])

    def test_checkpoint_resumes_from_breakpoint(self):
        # 只回了两台阀门的回执，第三台失败/未回：检查点记录断点
        item = self._receipt(self.item, "R-1", "V-1")
        item = self._receipt(item, "R-2", "V-2")
        self.assertEqual(item["status"], "isolating")
        self.assertEqual(item["checkpoint"]["pending"], ["V-3"])
        # 重复下发隔离命令：从断点续作，不影响已有回执
        resumed = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2", "V-3"]}, "s", "supervisor", item["version"])
        self.assertTrue(resumed["checkpoint"]["valves"][0]["state"] == "closed")
        self.assertEqual(resumed["checkpoint"]["pending"], ["V-3"])
        # 第三台回执到达后隔离完成
        item = self._receipt(resumed, "R-3", "V-3")
        self.assertEqual(item["status"], "isolated")
        self.assertTrue(item["checkpoint"]["complete"])

    def test_duplicate_receipt_does_not_reexecute(self):
        item = self._receipt(self.item, "R-1", "V-1")
        version_after_first = item["version"]
        # 同一回执重复提交：幂等忽略，不重复执行、不重复递增
        again = self.service.act(item["id"], "valve_receipt", {"receipts": [
            {"receipt_id": "R-1", "valve_id": "V-1", "result": "closed"}
        ]}, "s", "supervisor", item["version"])
        self.assertEqual(again["version"], version_after_first)
        self.assertEqual(again["checkpoint"]["pending"], ["V-2", "V-3"])
        self.assertEqual(again["valve_receipts"].__len__(), 1)

    def test_failed_valve_blocks_isolation(self):
        item = self._receipt(self.item, "R-1", "V-1")
        item = self._receipt(item, "R-2", "V-2", result="failed")
        self.assertEqual(item["status"], "isolating")
        self.assertTrue(item["payload"]["valve_status_conflict"])
        self.assertTrue(item["checkpoint"]["conflicts"])
        # 冲突未解除前不能继续隔离（断点续作会被冲突拦住）
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2", "V-3"]}, "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "valve_status_conflict")
        # 冲突未解除前不能抢修
        with self.assertRaises(DomainError) as context2:
            self.service.act(item["id"], "repair", {"work_order": "WO-X"}, "t", "technician", item["version"])
        self.assertEqual(context2.exception.code, "invalid_state")

    def test_cannot_change_sequence_during_isolation(self):
        with self.assertRaises(ConflictError):
            self.service.act(self.item["id"], "isolate", {"valve_sequence": ["V-9", "V-8"]}, "s", "supervisor", self.item["version"])

    def test_receipt_for_unknown_valve_rejected(self):
        with self.assertRaises(DomainError) as context:
            self._receipt(self.item, "R-X", "V-99")
        self.assertEqual(context.exception.code, "valve_not_in_sequence")


if __name__ == "__main__":
    unittest.main()
