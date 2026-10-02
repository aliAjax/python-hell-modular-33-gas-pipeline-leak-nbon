import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.rules import assess
from src.domain import DomainError


class RuleTest(unittest.TestCase):
    def test_leak_score_levels(self):
        critical = assess({"pressure_drop_kpa": 60, "sensor_value_ppm": 400, "odor_reports": 4})
        low = assess({"pressure_drop_kpa": 1, "sensor_value_ppm": 2, "odor_reports": 0})
        self.assertEqual(critical["level"], "critical")
        self.assertEqual(low["level"], "low")
        self.assertGreater(critical["score"], low["score"])

    def test_restore_requires_hazard_clearance(self):
        item = {
            "status": "tested",
            "payload": {"pressure_test": {"passed": True}, "valve_status_conflict": False},
        }
        from src.rules import apply_action
        with self.assertRaises(DomainError) as context:
            apply_action(item, "restore", {"hazards_clear": False}, "s", "supervisor")
        self.assertEqual(context.exception.code, "hazards_not_clear")

    def test_restore_reconciles_records_into_pending(self):
        from src.rules import apply_action
        item = {
            "status": "tested",
            "version": 8,
            "payload": {
                "pressure_test": {"passed": True, "pressure_kpa": 150, "minimum_pressure_kpa": 100},
                "isolation": {"sequence": ["V-1", "V-2"], "commands": {"V-1": {"state": "closed", "attempts": []}}},
            },
        }
        status, payload, event = apply_action(item, "restore", {}, "s", "supervisor")
        self.assertEqual(status, "pending")
        kinds = {m["type"] for m in payload["reconciliation"]["mismatches"]}
        self.assertEqual(kinds, {"valve_receipt", "hazard_clearance"})

    def test_valve_receipt_retry_keeps_closed_valves(self):
        from src.rules import apply_action
        item = {
            "status": "isolating",
            "version": 3,
            "payload": {
                "isolation": {
                    "sequence": ["V-1", "V-2"],
                    "commands": {
                        "V-1": {"state": "closed", "attempts": [{"command_id": "cmd-1"}]},
                        "V-2": {"state": "failed", "attempts": [{"command_id": "cmd-2"}]},
                    },
                }
            },
        }
        status, payload, event = apply_action(
            item, "valve_receipt", {"valve_id": "V-2", "command_id": "cmd-3", "result": "closed"}, "r", "responder"
        )
        self.assertEqual(status, "isolated")
        self.assertEqual(payload["isolation"]["commands"]["V-1"]["state"], "closed")
        self.assertEqual(len(payload["isolation"]["commands"]["V-2"]["attempts"]), 2)
        with self.assertRaises(DomainError) as context:
            apply_action(
                {"status": "isolating", "version": 4, "payload": payload},
                "valve_receipt", {"valve_id": "V-2", "command_id": "cmd-3", "result": "closed"}, "r", "responder",
            )
        self.assertEqual(context.exception.code, "duplicate_receipt")

    def test_verify_keeps_revision_history(self):
        from src.rules import apply_action
        item = {"status": "reported", "version": 1, "payload": {"pressure_drop_kpa": 10, "sensor_value_ppm": 30, "odor_reports": 0}}
        status, payload, _ = apply_action(item, "verify", {"field_confirmed": True, "note": "第一次"}, "r", "responder")
        self.assertEqual(status, "verified")
        item2 = {"status": "verified", "version": 2, "payload": payload}
        status, payload, _ = apply_action(item2, "verify", {"field_confirmed": True, "note": "复核", "source_ids": [1, 2]}, "r", "responder")
        self.assertEqual(len(payload["verifications"]), 2)
        self.assertEqual(payload["verifications"][0]["item_version"], 1)
        self.assertEqual(payload["verifications"][1]["source_ids"], [1, 2])
        self.assertEqual(payload["verification"]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
