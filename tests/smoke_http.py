import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = 8391
BASE = "http://127.0.0.1:%d" % PORT


def call(method, path, body=None, actor="u1", role="dispatcher"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-User-Id", actor)
    req.add_header("X-Role", role)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def start_server(db):
    proc = subprocess.Popen(
        [sys.executable, "app.py", "--db", db, "--port", str(PORT)],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            status, _ = call("GET", "/health")
            if status == 200:
                return proc
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def main():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    proc = start_server(db)
    try:
        # 1. 巡线员和传感网并发上报同一管段 -> 只能合成一条事件
        results = []

        def report(actor, role, reporter, ppm):
            body = {
                "pipeline_id": "P-9", "segment_id": "S-1",
                "reported_at": "2026-10-02T08:00:00+00:00",
                "pressure_drop_kpa": 25, "sensor_value_ppm": ppm,
                "odor_reports": 1, "reporter": reporter,
                "source_type": role, "external_id": "evt-" + reporter,
            }
            results.append(call("POST", "/api/items", body, actor, role))

        threads = [threading.Thread(target=report, args=("p1", "patrol", "patrol-1", 100)),
                   threading.Thread(target=report, args=("s1", "sensor", "sensor-grid", 300))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        codes = sorted(r[0] for r in results)
        ids = {r[1]["id"] for r in results}
        assert codes == [200, 201], codes
        assert len(ids) == 1, ids
        item_id = ids.pop()
        status, detail = call("GET", "/api/items/%d" % item_id)
        assert len(detail["sources"]) == 2, detail["sources"]
        assert detail["payload"]["sensor_value_ppm"] == 300  # 就高合并
        assert detail["payload"]["source_comparison"], "comparison missing"
        print("1. concurrent reports merged into item %d, sources=2, ppm=300 OK" % item_id)

        # 2. 完整流程：核验 -> 隔离 -> 回执(失败/重试/重复) -> 抢修 -> 试压 -> 对账
        v = detail["version"]
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "verify", "field_confirmed": True}, "r1", "responder")
        assert status == 200, item
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "isolate", "valve_sequence": ["V-1", "V-2"], "expected_version": item["version"]}, "s1", "supervisor")
        assert item["status"] == "isolating", item
        v = item["version"]
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "valve_receipt", "valve_id": "V-1", "command_id": "c1", "result": "closed", "expected_version": v}, "r1", "responder")
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "valve_receipt", "valve_id": "V-2", "command_id": "c2", "result": "failed", "expected_version": item["version"]}, "r1", "responder")
        assert item["status"] == "isolating"
        v = item["version"]
        # 重复回执 -> 不重复执行
        status, dup = call("POST", "/api/items/%d/actions" % item_id, {"action": "valve_receipt", "valve_id": "V-2", "command_id": "c2", "result": "failed", "expected_version": v}, "r1", "responder")
        assert dup["version"] == v, (dup["version"], v)
        # 断点重试
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "valve_receipt", "valve_id": "V-2", "command_id": "c3", "result": "closed", "expected_version": v}, "r1", "responder")
        assert item["status"] == "isolated", item
        print("2. isolation retry from breakpoint + idempotent receipt OK")

        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "repair", "work_order": "WO-1", "expected_version": item["version"]}, "t1", "technician")
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "pressure_test", "test_passed": True, "pressure_kpa": 150, "minimum_pressure_kpa": 100, "expected_version": item["version"]}, "t1", "technician")
        # 缺危险解除记录 -> 对账不上 -> 待处理
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "restore", "expected_version": item["version"]}, "s1", "supervisor")
        assert item["status"] == "pending", item
        assert item["payload"]["reconciliation"]["mismatches"][0]["type"] == "hazard_clearance"
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "clear_hazard", "expected_version": item["version"]}, "r1", "responder")
        status, item = call("POST", "/api/items/%d/actions" % item_id, {"action": "restore", "expected_version": item["version"]}, "s1", "supervisor")
        assert item["status"] == "restored", item
        print("3. reconciliation mismatch -> pending -> restored OK")

        status, audit = call("GET", "/api/items/%d/audit" % item_id)
        assert audit["chain_valid"] if False else audit["chain_valid"], audit
        print("4. audit chain valid, events=%d OK" % len(audit["events"]))
    finally:
        proc.terminate()
        proc.wait()

    # 5. 重启后状态、版本、审计链连续
    proc = start_server(db)
    try:
        status, item = call("GET", "/api/items/%d" % item_id)
        assert item["status"] == "restored", item["status"]
        status, audit = call("GET", "/api/items/%d/audit" % item_id)
        assert audit["chain_valid"]
        print("5. after restart: status=restored version=%d chain_valid OK" % item["version"])
    finally:
        proc.terminate()
        proc.wait()
    os.unlink(db)
    print("smoke test passed")


if __name__ == "__main__":
    main()
