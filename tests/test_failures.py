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

    def test_same_segment_reports_merge_into_one_item(self):
        item, created = self.service.create_item(self.payload, "d", "dispatcher")
        self.assertTrue(created)
        again, created_again = self.service.create_item(self.payload, "d", "dispatcher")
        self.assertFalse(created_again)
        self.assertEqual(again["id"], item["id"])
        # 完全重复的上报是幂等的，不新增来源
        self.assertEqual(len(self.service.get_item(item["id"])["sources"]), 1)
        sensor_report = dict(self.payload)
        sensor_report.update({
            "reporter": "sensor-grid",
            "source_type": "sensor",
            "external_id": "evt-9001",
            "reported_at": "2026-09-27T09:00:30+00:00",
            "sensor_value_ppm": 260,
        })
        merged, created_merged = self.service.create_item(sensor_report, "s", "sensor")
        self.assertFalse(created_merged)
        self.assertEqual(merged["id"], item["id"])
        detail = self.service.get_item(item["id"])
        self.assertEqual(len(detail["sources"]), 2)
        self.assertEqual(detail["payload"]["sensor_value_ppm"], 260)
        self.assertTrue(detail["payload"]["source_comparison"])

    def test_valve_sequence_and_version_conflict(self):
        item, _ = self.service.create_item(self.payload, "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1"]}, "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "valve_sequence_required")
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"])
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "valve_receipt", {"valve_id": "V-1", "command_id": "cmd-1", "result": "closed"}, "r", "responder", item["version"] - 1)

    def test_version_conflict_and_permission(self):
        item, _ = self.service.create_item(self.payload, "d", "dispatcher")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "verify", {"field_confirmed": True}, "x", "sensor", item["version"])
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"] - 1)


if __name__ == "__main__":
    unittest.main()
