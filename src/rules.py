from datetime import datetime, timezone

from .domain import DomainError

ENTITY_TYPE = "pipeline_leak"
INITIAL_STATUS = "reported"
TERMINAL_STATUSES = {"restored", "cancelled"}
CREATE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
SOURCE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
ACTION_ROLES = {
    "verify": {"dispatcher", "responder"},
    "isolate": {"supervisor", "responder"},
    "valve_receipt": {"supervisor", "responder", "sensor"},
    "repair": {"technician"},
    "pressure_test": {"technician"},
    "clear_hazard": {"responder", "supervisor"},
    "restore": {"supervisor"},
    "cancel": {"supervisor"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"isolate", "valve_receipt", "repair", "pressure_test", "clear_hazard", "restore", "cancel"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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


def receipt_seen(item, command_id):
    isolation = item["payload"].get("isolation") or {}
    for command in (isolation.get("commands") or {}).values():
        for attempt in command.get("attempts", []):
            if attempt.get("command_id") == command_id:
                return True
    return False


def _apply_verify(item, payload, actor):
    current = dict(item["payload"])
    if item["status"] in TERMINAL_STATUSES:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])
    confirmed = bool(payload.get("field_confirmed"))
    if not confirmed:
        raise DomainError("field_confirmation_required", "需要现场确认", 409)
    resolved_conflict = False
    if current.get("valve_status_conflict"):
        if not payload.get("valve_status_confirmed"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，需要现场确认阀门状态", 409)
        current["valve_status_conflict"] = False
        resolved_conflict = True
    revisions = list(current.get("verifications") or [])
    source_ids = payload.get("source_ids") or []
    if not isinstance(source_ids, list):
        raise DomainError("invalid_source_ids", "source_ids 必须是列表")
    revision = {
        "revision": len(revisions) + 1,
        "item_version": item["version"],
        "actor": actor,
        "note": payload.get("note", ""),
        "assessment": assess(current),
        "source_ids": source_ids,
        "valve_status_resolved": resolved_conflict,
        "recorded_at": now_iso(),
    }
    revisions.append(revision)
    current["verifications"] = revisions
    current["assessment"] = revision["assessment"]
    current["verification"] = {"confirmed": True, "note": revision["note"], "revision": revision["revision"]}
    new_status = "verified" if item["status"] in {"reported", "verified"} else item["status"]
    return new_status, current, {"verification": revision}


def _apply_isolate(item, payload, actor):
    current = dict(item["payload"])
    _need_status(item, {"verified"})
    sequence = payload.get("valve_sequence")
    if not isinstance(sequence, list) or len(sequence) < 2:
        raise DomainError("valve_sequence_required", "至少需要提交两个阀门及顺序")
    if not all(isinstance(value, str) and value.strip() for value in sequence):
        raise DomainError("invalid_valve_sequence", "阀门顺序格式无效")
    if current.get("valve_status_conflict"):
        raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能隔离", 409)
    sequence = [value.strip() for value in sequence]
    current["valve_sequence"] = sequence
    current["isolation"] = {
        "sequence": sequence,
        "commands": {},
        "started_by": actor,
        "started_at": now_iso(),
    }
    return "isolating", current, {"valve_sequence": sequence}


def _apply_valve_receipt(item, payload, actor):
    current = dict(item["payload"])
    _need_status(item, {"isolating", "isolated", "repaired", "tested", "pending"})
    isolation = current.get("isolation") or {}
    sequence = isolation.get("sequence") or []
    if not sequence:
        raise DomainError("isolation_not_started", "尚未下发隔离命令")
    valve_id = _text(payload, "valve_id")
    command_id = _text(payload, "command_id")
    result = payload.get("result")
    if result not in ("closed", "failed"):
        raise DomainError("invalid_receipt_result", "回执结果必须是 closed 或 failed")
    commands = dict(isolation.get("commands") or {})
    for command in commands.values():
        for attempt in command.get("attempts", []):
            if attempt.get("command_id") == command_id:
                raise DomainError("duplicate_receipt", "重复回执已被忽略", 409)
    attempt = {"command_id": command_id, "result": result, "actor": actor, "received_at": now_iso()}
    conflicts = list(current.get("valve_conflicts") or [])
    if valve_id not in sequence:
        conflicts.append({"valve_id": valve_id, "command_id": command_id,
                          "reason": "receipt_for_unknown_valve", "recorded_at": now_iso()})
        current["valve_conflicts"] = conflicts
        current["valve_status_conflict"] = True
        isolation["commands"] = commands
        current["isolation"] = isolation
        return item["status"], current, {"valve_id": valve_id, "command_id": command_id,
                                         "conflict": "receipt_for_unknown_valve"}
    command = dict(commands.get(valve_id) or {"state": "pending", "attempts": []})
    previous_state = command["state"]
    command["attempts"] = list(command.get("attempts") or []) + [attempt]
    if previous_state == "closed" and result != "closed":
        conflicts.append({"valve_id": valve_id, "command_id": command_id,
                          "reason": "contradicts_closed_state", "recorded_at": now_iso()})
        current["valve_conflicts"] = conflicts
        current["valve_status_conflict"] = True
    else:
        command["state"] = "closed" if result == "closed" else "failed"
    command["updated_at"] = now_iso()
    commands[valve_id] = command
    isolation["commands"] = commands
    current["isolation"] = isolation
    all_closed = all((commands.get(valve) or {}).get("state") == "closed" for valve in sequence)
    new_status = item["status"]
    if item["status"] == "isolating" and all_closed:
        new_status = "isolated"
    return new_status, current, {
        "valve_id": valve_id,
        "command_id": command_id,
        "result": result,
        "valve_state": command["state"],
        "all_closed": all_closed,
    }


def _apply_repair(item, payload, actor):
    current = dict(item["payload"])
    _need_status(item, {"isolated", "repaired"})
    work_order = _text(payload, "work_order")
    current["repair"] = {"work_order": work_order, "result": payload.get("result", "completed")}
    return "repaired", current, {"work_order": work_order}


def _apply_pressure_test(item, payload, actor):
    current = dict(item["payload"])
    _need_status(item, {"repaired", "tested", "pending"})
    if not payload.get("test_passed"):
        raise DomainError("pressure_test_failed", "压力测试未通过，不能恢复供气", 409)
    pressure = float(payload.get("pressure_kpa", 0))
    minimum = float(payload.get("minimum_pressure_kpa", 100))
    if pressure < minimum:
        raise DomainError("pressure_below_threshold", "试验压力低于最低要求", 409)
    current["pressure_test"] = {"passed": True, "pressure_kpa": pressure, "minimum_pressure_kpa": minimum}
    return "tested", current, {"pressure_test": current["pressure_test"]}


def _apply_clear_hazard(item, payload, actor):
    current = dict(item["payload"])
    _need_status(item, {"repaired", "tested", "pending"})
    current["hazard_clearance"] = {
        "cleared": True,
        "actor": actor,
        "note": payload.get("note", ""),
        "recorded_at": now_iso(),
    }
    return item["status"], current, {"hazard_clearance": current["hazard_clearance"]}


def _reconcile(current):
    mismatches = []
    isolation = current.get("isolation") or {}
    commands = isolation.get("commands") or {}
    for valve in isolation.get("sequence") or current.get("valve_sequence") or []:
        state = (commands.get(valve) or {}).get("state")
        if state != "closed":
            mismatches.append({"type": "valve_receipt", "valve_id": valve, "state": state or "missing"})
    if current.get("valve_status_conflict"):
        mismatches.append({"type": "valve_status_conflict", "detail": "阀门状态与现场确认不一致"})
    test = current.get("pressure_test") or {}
    if not test.get("passed"):
        mismatches.append({"type": "pressure_test", "detail": "缺少通过的压力测试"})
    elif float(test.get("pressure_kpa", 0)) < float(test.get("minimum_pressure_kpa", 100)):
        mismatches.append({"type": "pressure_test", "detail": "试验压力低于最低要求"})
    if not (current.get("hazard_clearance") or {}).get("cleared"):
        mismatches.append({"type": "hazard_clearance", "detail": "缺少危险解除记录"})
    return mismatches


def _apply_restore(item, payload, actor):
    current = dict(item["payload"])
    _need_status(item, {"tested", "pending"})
    if payload.get("hazards_clear") is False:
        raise DomainError("hazards_not_clear", "现场危险条件尚未解除", 409)
    mismatches = _reconcile(current)
    if mismatches:
        current["reconciliation"] = {
            "matched": False,
            "mismatches": mismatches,
            "actor": actor,
            "recorded_at": now_iso(),
        }
        return "pending", current, {"reconciliation": current["reconciliation"]}
    current["hazards_clear"] = True
    current["reconciliation"] = {
        "matched": True,
        "checked": ["valve_receipts", "pressure_test", "hazard_clearance"],
        "actor": actor,
        "recorded_at": now_iso(),
    }
    current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
    return "restored", current, {"reconciliation": current["reconciliation"], "restoration": current["restoration"]}


def _apply_cancel(item, payload, actor):
    current = dict(item["payload"])
    _need_status(item, {"reported", "verified"})
    reason = _text(payload, "reason")
    current["cancellation"] = {"reason": reason, "actor": actor}
    return "cancelled", current, {"reason": reason}


ACTION_HANDLERS = {
    "verify": _apply_verify,
    "isolate": _apply_isolate,
    "valve_receipt": _apply_valve_receipt,
    "repair": _apply_repair,
    "pressure_test": _apply_pressure_test,
    "clear_hazard": _apply_clear_hazard,
    "restore": _apply_restore,
    "cancel": _apply_cancel,
}


def apply_action(item, action, payload, actor, role):
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        raise DomainError("unknown_action", "不支持的操作")
    return handler(item, payload, actor)
