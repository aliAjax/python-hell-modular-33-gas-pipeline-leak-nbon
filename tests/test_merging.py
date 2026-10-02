import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class MergingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _payload(self, **overrides):
        base = {
            "pipeline_id": "P-2",
            "segment_id": "S-4",
            "reported_at": "2026-09-27T09:00:00+00:00",
            "pressure_drop_kpa": 20,
            "sensor_value_ppm": 50,
            "odor_reports": 1,
            "reporter": "dispatch-2",
        }
        base.update(overrides)
        return base

    def test_concurrent_alarms_merge_into_one_event(self):
        patrol = self.service.create_item(self._payload(reporter="patrol-9", source_type="patrol", external_id="PAT-9"), "p", "patrol")
        sensor = self.service.create_item(
            self._payload(reported_at="2026-09-27T09:02:00+00:00", pressure_drop_kpa=35, sensor_value_ppm=200, odor_reports=2,
                          reporter="sensor-net", source_type="sensor", external_id="SN-001"),
            "s", "sensor",
        )
        self.assertEqual(sensor["id"], patrol["id"])
        self.assertTrue(sensor["merged"])
        types = {(s["source_type"], s["external_id"]) for s in sensor["sources"]}
        self.assertIn(("patrol", "PAT-9"), types)
        self.assertIn(("sensor", "SN-001"), types)
        # 读数取最不利值，修订来源可追溯
        self.assertEqual(sensor["payload"]["pressure_drop_kpa"], 35)
        self.assertEqual(sensor["payload"]["sensor_value_ppm"], 200)
        self.assertEqual(sensor["payload"]["odor_reports"], 2)
        self.assertEqual(len(sensor["payload"]["merged_reports"]), 2)

    def test_same_report_is_idempotent(self):
        first = self.service.create_item(self._payload(), "d", "dispatcher")
        second = self.service.create_item(self._payload(), "d", "dispatcher")
        self.assertTrue(second["deduped"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(second["sources"]), 1)

    def test_terminal_item_does_not_merge(self):
        item = self.service.create_item(self._payload(), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "cancel", {"reason": "误报"}, "s", "supervisor", item["version"])
        self.assertEqual(item["status"], "cancelled")
        new_alarm = self.service.create_item(self._payload(reported_at="2026-09-28T10:00:00+00:00"), "d", "dispatcher")
        self.assertNotEqual(new_alarm["id"], item["id"])
        self.assertEqual(new_alarm["status"], "reported")

    def test_revision_keeps_source_and_version(self):
        item = self.service.create_item(self._payload(), "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True, "note": "初次核验"}, "r1", "responder", item["version"])
        self.assertEqual(len(item["payload"]["verifications"]), 1)
        # 新确认修订旧判断：保留来源与版本轨迹
        item = self.service.act(item["id"], "verify", {"field_confirmed": True, "note": "二次现场确认"}, "r2", "responder", item["version"])
        self.assertEqual(len(item["payload"]["verifications"]), 2)
        self.assertEqual(item["payload"]["verifications"][1]["actor"], "r2")
        self.assertEqual(item["payload"]["verifications"][1]["role"], "responder")
        self.assertIn("assessment_before", item["payload"]["verifications"][1])
        self.assertGreater(item["version"], 2)
        self.assertTrue(item["audit_chain"]["valid"])


if __name__ == "__main__":
    unittest.main()
