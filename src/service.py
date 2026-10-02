from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        segment_key = normalized.pop("_segment_key")
        stable_key = normalized.pop("_stable_key")
        source_type = normalized["source_type"]
        external_id = normalized["external_id"]

        # 同一管段存在在途事件：并入该事件，来源并存，不再重复建单
        active = self.repository.find_active_by_segment(
            rules.ENTITY_TYPE, segment_key, rules.TERMINAL_STATUSES
        )
        if active is not None:
            # 同一来源的同一报告重复提交：幂等返回，不重复并入
            if self.repository.source_exists(active["id"], source_type, external_id):
                result = self.get_item(active["id"])
                result["deduped"] = True
                return result
            source = {
                "source_type": source_type,
                "external_id": external_id,
                "observed_at": normalized["reported_at"],
                "payload": {"reporter": normalized["reporter"], "note": payload.get("note", "")},
            }
            readings = {
                "pressure_drop_kpa": normalized["pressure_drop_kpa"],
                "sensor_value_ppm": normalized["sensor_value_ppm"],
                "odor_reports": normalized["odor_reports"],
            }
            self.repository.merge_alarm(
                rules.ENTITY_TYPE, active["id"], segment_key, source, readings, actor, role
            )
            result = self.get_item(active["id"])
            result["merged"] = True
            return result

        # 没有在途事件：若稳定键已存在（如终态事件的重复报告），幂等返回
        existing = self.repository.find_by_stable_key(rules.ENTITY_TYPE, stable_key)
        if existing is not None:
            result = self.get_item(existing["id"])
            result["deduped"] = True
            return result

        item = self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, segment_key, rules.INITIAL_STATUS, normalized, actor, role
        )
        return self.get_item(item["id"])

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        try:
            new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        except DomainError as exc:
            # 对账失败等场景：保留 partial 结果（留在待处理），落库后原样抛出
            if getattr(exc, "partial", None) is None:
                raise
            new_status, new_payload, event_payload = exc.partial
            updated = self.repository.apply_action(
                item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
            )
            exc.current_version = updated["version"]
            raise
        # 重复回执是 no-op：不重复执行、不递增版本，仅回显幂等结果
        if action == "valve_receipt" and event_payload.get("fresh_count", 0) == 0:
            result = self.get_item(item_id)
            result["duplicates"] = event_payload.get("duplicates", [])
            result["noop"] = True
            return result
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["valve_receipts"] = self.repository.list_valve_receipts(item_id)
        item["checkpoint"] = item["payload"].get("valve_checkpoint")
        item["audit_chain"] = self.repository.verify_chain(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
