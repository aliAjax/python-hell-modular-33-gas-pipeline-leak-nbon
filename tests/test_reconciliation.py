import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError
from src.rules import _reconcile


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _ready_item(self, pressure=True, hazard=False):
        # 隔离必须先完成（回执到齐），才能抢修、试压
        item = self.service.create_item({
            "pipeline_id": "P-4",
            "segment_id": "S-2",
            "reported_at": "2026-09-27T11:00:00+00:00",
            "pressure_drop_kpa": 30,
            "sensor_value_ppm": 90,
            "odor_reports": 1,
            "reporter": "dispatch-4",
        }, "d", "dispatcher")
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "r", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", item["version"])
        for i, valve in enumerate(["V-1", "V-2"], 1):
            item = self.service.act(item["id"], "valve_receipt", {"receipts": [
                {"receipt_id": "R-%d" % i, "valve_id": valve, "result": "closed"}
            ]}, "s", "supervisor", item["version"])
        self.assertEqual(item["status"], "isolated")
        item = self.service.act(item["id"], "repair", {"work_order": "WO-9"}, "t", "technician", item["version"])
        if pressure:
            item = self.service.act(item["id"], "pressure_test", {"test_passed": True, "pressure_kpa": 150, "minimum_pressure_kpa": 100}, "t", "technician", item["version"])
        if hazard:
            item = self.service.act(item["id"], "hazard_clear", {"cleared": True}, "s", "supervisor", item["version"])
        return item

    def test_reconcile_failure_stays_pending_with_mismatches(self):
        # 已试压但危险未解除：对账不通过，留在待处理（tested）
        item = self._ready_item(pressure=True, hazard=False)
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "reconcile", {}, "s", "supervisor", item["version"])
        exc = context.exception
        self.assertEqual(exc.code, "reconciliation_failed")
        self.assertIn("hazard_not_cleared", {m["code"] for m in exc.mismatches})
        # 失败记录已落库，状态仍为 tested（待处理）
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "tested")
        self.assertFalse(item["payload"]["reconciliation"]["passed"])
        # 安全条件没解除前不能恢复供气
        with self.assertRaises(DomainError) as context2:
            self.service.act(item["id"], "restore", {"hazards_clear": True}, "s", "supervisor", item["version"])
        self.assertEqual(context2.exception.code, "reconciliation_failed")
        self.assertEqual(self.service.get_item(item["id"])["status"], "tested")

    def test_reconcile_passes_after_conditions_met(self):
        item = self._ready_item(pressure=True, hazard=True)
        item = self.service.act(item["id"], "reconcile", {}, "s", "supervisor", item["version"])
        self.assertEqual(item["status"], "reconciled")
        item = self.service.act(item["id"], "restore", {"hazards_clear": True}, "s", "supervisor", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_restore_requires_hazard_clearance(self):
        item = self._ready_item(pressure=True, hazard=True)
        item = self.service.act(item["id"], "reconcile", {}, "s", "supervisor", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"hazards_clear": False}, "s", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "hazards_not_clear")

    def test_reconcile_detects_valve_receipt_mismatches(self):
        # 纯函数：对账按阀门回执核对，缺回执或回执失败都应暴露
        base = {
            "valve_sequence": ["V-1", "V-2"],
            "pressure_test": {"passed": True},
            "hazard_clearance": {"cleared": True},
        }
        missing = dict(base, valve_receipts=[{"receipt_id": "R-1", "valve_id": "V-1", "result": "closed"}])
        codes = {m["code"] for m in _reconcile(missing)}
        self.assertIn("valve_receipt_missing", codes)

        failed = dict(base, valve_receipts=[
            {"receipt_id": "R-1", "valve_id": "V-1", "result": "closed"},
            {"receipt_id": "R-2", "valve_id": "V-2", "result": "failed"},
        ])
        codes = {m["code"] for m in _reconcile(failed)}
        self.assertIn("valve_receipt_failed", codes)

        conflicting = dict(base, valve_receipts=[
            {"receipt_id": "R-1", "valve_id": "V-1", "result": "closed"},
            {"receipt_id": "R-2", "valve_id": "V-1", "result": "failed"},
        ])
        codes = {m["code"] for m in _reconcile(conflicting)}
        self.assertIn("valve_status_conflict", codes)

        no_pressure = dict(base, valve_receipts=[
            {"receipt_id": "R-1", "valve_id": "V-1", "result": "closed"},
            {"receipt_id": "R-2", "valve_id": "V-2", "result": "closed"},
        ], pressure_test={})
        codes = {m["code"] for m in _reconcile(no_pressure)}
        self.assertIn("pressure_test_missing", codes)

        no_hazard = dict(base, valve_receipts=[
            {"receipt_id": "R-1", "valve_id": "V-1", "result": "closed"},
            {"receipt_id": "R-2", "valve_id": "V-2", "result": "closed"},
        ], hazard_clearance={})
        codes = {m["code"] for m in _reconcile(no_hazard)}
        self.assertIn("hazard_not_cleared", codes)

        ok = dict(base, valve_receipts=[
            {"receipt_id": "R-1", "valve_id": "V-1", "result": "closed"},
            {"receipt_id": "R-2", "valve_id": "V-2", "result": "closed"},
        ])
        self.assertEqual(_reconcile(ok), [])


if __name__ == "__main__":
    unittest.main()
