import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "pipeline_id": "P-2",
            "segment_id": "S-4",
            "reported_at": "2026-09-27T09:00:00+00:00",
            "pressure_drop_kpa": 20,
            "sensor_value_ppm": 50,
            "odor_reports": 1,
            "reporter": "dispatch-2",
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_duplicate_and_valve_conflict(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        # 同一管段在途：第二起报警并入同一事件，来源并存
        merged = self.service.create_item(dict(self.payload, reporter="patrol-1"), "d", "dispatcher")
        self.assertEqual(merged["id"], item["id"])
        self.assertTrue(merged["merged"])
        self.assertGreaterEqual(len(merged["sources"]), 2)
        # 完全相同的报告重复提交：幂等，不再并入来源
        deduped = self.service.create_item(self.payload, "d", "dispatcher")
        self.assertTrue(deduped["deduped"])
        self.assertEqual(len(deduped["sources"]), len(merged["sources"]))
        item["payload"]["valve_status_conflict"] = False
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", merged["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1"]}, "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "valve_sequence_required")

    def test_version_conflict_and_permission(self):
        item = self.service.create_item(self.payload, "d", "dispatcher")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", {"field_confirmed": True}, "x", "sensor", item["version"])
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"] - 1)


if __name__ == "__main__":
    unittest.main()
