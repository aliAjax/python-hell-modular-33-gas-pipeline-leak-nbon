from .domain import DomainError, ConflictError

ENTITY_TYPE = "pipeline_leak"
INITIAL_STATUS = "reported"
# 终态：已恢复或已取消，不再参与在途报警合并
TERMINAL_STATUSES = {"restored", "cancelled"}
CREATE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
SOURCE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
ACTION_ROLES = {
    "verify": {"dispatcher", "responder"},
    "isolate": {"supervisor", "responder"},
    "valve_receipt": {"supervisor", "responder"},
    "repair": {"technician"},
    "pressure_test": {"technician"},
    "hazard_clear": {"supervisor"},
    "reconcile": {"supervisor"},
    "restore": {"supervisor"},
    "cancel": {"supervisor"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {
    "isolate",
    "valve_receipt",
    "repair",
    "pressure_test",
    "hazard_clear",
    "reconcile",
    "restore",
    "cancel",
}


def assess(payload):
    pressure = float(payload.get("pressure_drop_kpa", 0))
    ppm = float(payload.get("sensor_value_ppm", 0))
    odor = int(payload.get("odor_reports", 0))
    score = min(100.0, pressure * 2.0 + min(ppm, 500.0) * 0.1 + odor * 5.0)
    if score >= 75:
        level = "critical"
    elif score >= 45:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _checkpoint(current):
    """根据阀门回执计算隔离断点：每台阀门的最新状态、待关闭清单与冲突。"""
    sequence = current.get("valve_sequence") or []
    receipts = current.get("valve_receipts") or []
    latest = {}
    for receipt in receipts:
        latest[receipt["valve_id"]] = receipt
    valves = []
    for valve_id in sequence:
        receipt = latest.get(valve_id)
        state = receipt["result"] if receipt else "pending"
        valves.append({"valve_id": valve_id, "state": state})
    conflicts = []
    by_valve = {}
    for receipt in receipts:
        by_valve.setdefault(receipt["valve_id"], []).append(receipt["result"])
    for valve_id, results in by_valve.items():
        if "closed" in results and "failed" in results:
            conflicts.append({"code": "valve_result_conflict", "valve_id": valve_id})
    for valve in valves:
        if valve["state"] == "failed":
            conflicts.append({"code": "valve_failed", "valve_id": valve["valve_id"]})
    pending = [v["valve_id"] for v in valves if v["state"] != "closed"]
    complete = bool(sequence) and not conflicts and not pending
    return {"valves": valves, "pending": pending, "complete": complete, "conflicts": conflicts}


def _reconcile(current):
    """对账：阀门回执（全关且无冲突）、压力测试、危险解除记录，缺一不可。"""
    mismatches = []
    sequence = current.get("valve_sequence")
    if not sequence:
        mismatches.append({"code": "valve_sequence_missing", "message": "缺少阀门隔离顺序"})
    else:
        checkpoint = _checkpoint(current)
        for valve in checkpoint["valves"]:
            if valve["state"] == "pending":
                mismatches.append({
                    "code": "valve_receipt_missing",
                    "message": "阀门 %s 缺少关闭回执" % valve["valve_id"],
                    "valve_id": valve["valve_id"],
                })
            elif valve["state"] == "failed":
                mismatches.append({
                    "code": "valve_receipt_failed",
                    "message": "阀门 %s 回执为失败" % valve["valve_id"],
                    "valve_id": valve["valve_id"],
                })
        for conflict in checkpoint["conflicts"]:
            mismatches.append({
                "code": "valve_status_conflict",
                "message": "阀门 %s 状态冲突" % conflict.get("valve_id"),
                "valve_id": conflict.get("valve_id"),
            })
    if not (current.get("pressure_test") or {}).get("passed"):
        mismatches.append({"code": "pressure_test_missing", "message": "缺少通过的压力测试"})
    if not (current.get("hazard_clearance") or {}).get("cleared"):
        mismatches.append({"code": "hazard_not_cleared", "message": "现场危险条件尚未解除"})
    return mismatches


def _fail_with_partial(code, message, status, new_status, current, event_payload, mismatches):
    """对账失败但需要保留失败记录（留在待处理），由仓储落库后再抛出。"""
    exc = DomainError(code, message, status, mismatches=mismatches)
    exc.partial = (new_status, current, event_payload)
    raise exc


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"reported", "verified"})
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能完成核验", 409)
        confirmed = bool(payload.get("field_confirmed"))
        if not confirmed:
            raise DomainError("field_confirmation_required", "需要现场确认", 409)
        before = current.get("assessment")
        after = assess(current)
        current["assessment"] = after
        revision = {
            "confirmed": True,
            "note": payload.get("note", ""),
            "actor": actor,
            "role": role,
            "assessment_before": before,
            "assessment_after": after,
        }
        current.setdefault("verifications", []).append(revision)
        current["verification"] = {"confirmed": True, "note": payload.get("note", "")}
        return "verified", current, {
            "assessment": after,
            "revision": revision,
            "revised": before is not None,
        }

    if action == "isolate":
        _need_status(item, {"verified", "isolating"})
        sequence = payload.get("valve_sequence")
        if not isinstance(sequence, list) or len(sequence) < 2:
            raise DomainError("valve_sequence_required", "至少需要提交两个阀门及顺序")
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能隔离", 409)
        if not all(isinstance(value, str) and value.strip() for value in sequence):
            raise DomainError("invalid_valve_sequence", "阀门顺序格式无效")
        sequence = [value.strip() for value in sequence]
        if status == "isolating":
            if sequence != current.get("valve_sequence"):
                raise ConflictError("isolation_in_progress", "隔离进行中，不能更换阀门顺序")
            # 断点续作：不重复下发已确认的阀门，只返回当前检查点
            checkpoint = _checkpoint(current)
            current["valve_checkpoint"] = checkpoint
            return "isolating", current, {"valve_sequence": sequence, "checkpoint": checkpoint, "resumed": True}
        current["valve_sequence"] = sequence
        current["valve_receipts"] = []
        checkpoint = _checkpoint(current)
        current["valve_checkpoint"] = checkpoint
        return "isolating", current, {"valve_sequence": sequence, "checkpoint": checkpoint, "resumed": False}

    if action == "valve_receipt":
        _need_status(item, {"isolating", "isolated"})
        receipts = payload.get("receipts")
        if not isinstance(receipts, list) or not receipts:
            raise DomainError("receipts_required", "缺少阀门回执", 400)
        sequence = current.get("valve_sequence") or []
        normalized = []
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise DomainError("invalid_receipt", "回执格式无效", 400)
            receipt_id = receipt.get("receipt_id")
            valve_id = receipt.get("valve_id")
            result = receipt.get("result", "closed")
            if not isinstance(receipt_id, str) or not receipt_id.strip():
                raise DomainError("receipt_id_required", "回执缺少 receipt_id", 400)
            if not isinstance(valve_id, str) or not valve_id.strip():
                raise DomainError("valve_id_required", "回执缺少 valve_id", 400)
            if result not in ("closed", "failed"):
                raise DomainError("invalid_receipt_result", "回执结果必须是 closed 或 failed", 400)
            valve_id = valve_id.strip()
            if valve_id not in sequence:
                raise DomainError("valve_not_in_sequence", "阀门 %s 不在隔离顺序中" % valve_id, 400)
            normalized.append({
                "receipt_id": receipt_id.strip(),
                "valve_id": valve_id,
                "result": result,
                "note": receipt.get("note", ""),
                "actor": actor,
            })
        existing_ids = {r["receipt_id"] for r in current.get("valve_receipts", [])}
        duplicates = [r["receipt_id"] for r in normalized if r["receipt_id"] in existing_ids]
        fresh = [r for r in normalized if r["receipt_id"] not in existing_ids]
        current.setdefault("valve_receipts", []).extend(fresh)
        checkpoint = _checkpoint(current)
        current["valve_checkpoint"] = checkpoint
        if checkpoint["conflicts"]:
            current["valve_status_conflict"] = True
            return "isolating", current, {"checkpoint": checkpoint, "duplicates": duplicates, "fresh_count": len(fresh)}
        current["valve_status_conflict"] = False
        new_status = "isolated" if checkpoint["complete"] else "isolating"
        return new_status, current, {"checkpoint": checkpoint, "duplicates": duplicates, "fresh_count": len(fresh)}

    if action == "repair":
        _need_status(item, {"isolated", "repaired"})
        work_order = _text(payload, "work_order")
        current["repair"] = {"work_order": work_order, "result": payload.get("result", "completed")}
        return "repaired", current, {"work_order": work_order}

    if action == "pressure_test":
        _need_status(item, {"repaired", "tested"})
        if not payload.get("test_passed"):
            raise DomainError("pressure_test_failed", "压力测试未通过，不能恢复供气", 409)
        pressure = float(payload.get("pressure_kpa", 0))
        minimum = float(payload.get("minimum_pressure_kpa", 100))
        if pressure < minimum:
            raise DomainError("pressure_below_threshold", "试验压力低于最低要求", 409)
        current["pressure_test"] = {"passed": True, "pressure_kpa": pressure, "minimum_pressure_kpa": minimum}
        return "tested", current, {"pressure_test": current["pressure_test"]}

    if action == "hazard_clear":
        _need_status(item, {"repaired", "tested"})
        if not bool(payload.get("cleared")):
            raise DomainError("hazard_clearance_required", "需要确认现场危险条件已解除", 409)
        current["hazard_clearance"] = {
            "cleared": True,
            "actor": actor,
            "role": role,
            "note": payload.get("note", ""),
        }
        return status, current, {"hazard_clearance": current["hazard_clearance"]}

    if action == "reconcile":
        _need_status(item, {"tested"})
        mismatches = _reconcile(current)
        current["reconciliation"] = {"passed": not mismatches, "mismatches": mismatches, "actor": actor}
        if mismatches:
            # 对账不通过：留在待处理（tested），记录失败明细，落库后抛出
            _fail_with_partial(
                "reconciliation_failed",
                "对账未通过，事件留在待处理：" + "；".join(m["message"] for m in mismatches),
                409,
                status,
                current,
                {"mismatches": mismatches},
                mismatches,
            )
        return "reconciled", current, {"mismatches": []}

    if action == "restore":
        _need_status(item, {"tested", "reconciled"})
        if not payload.get("hazards_clear"):
            raise DomainError("hazards_not_clear", "现场危险条件尚未解除", 409)
        if not current.get("pressure_test", {}).get("passed"):
            raise DomainError("pressure_test_missing", "缺少通过的压力测试", 409)
        mismatches = _reconcile(current)
        if mismatches:
            current["reconciliation"] = {"passed": False, "mismatches": mismatches, "actor": actor}
            _fail_with_partial(
                "reconciliation_failed",
                "安全对账未通过，不能恢复供气：" + "；".join(m["message"] for m in mismatches),
                409,
                status,
                current,
                {"mismatches": mismatches},
                mismatches,
            )
        current["hazards_clear"] = True
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"reported", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
