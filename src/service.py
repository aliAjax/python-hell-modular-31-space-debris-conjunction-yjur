import uuid

from . import domain, rules, notifications
from .domain import DomainError


class Service:
    def __init__(self, repository, notifier=None):
        self.repository = repository
        self.notifier = notifier or notifications.LoggingNotifier()

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        # 创建时录入的轨道参数构成第 0 版当前依据。
        normalized["current_basis"] = {
            "basis_version": 0,
            "basis_source_id": None,
            "source_type": "create",
            "external_id": None,
            "observed_at": None,
        }
        normalized["blocked_actions"] = []
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None, expected_version=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        source_type = normalized.pop("source_type")
        external_id = normalized.pop("external_id")
        observed_at = normalized.pop("observed_at")
        source_payload = normalized

        def decide(current, existing_sources, new_source_payload, new_source_id):
            new_source = {
                "source_type": source_type,
                "external_id": external_id,
                "observed_at": observed_at,
                "miss_distance_m": new_source_payload["miss_distance_m"],
                "covariance_m": new_source_payload["covariance_m"],
            }
            return rules.source_decision(
                current, existing_sources, new_source, new_source_id, item["status"]
            )

        source, decision = self.repository.ingest_source(
            item_id,
            {"source_type": source_type, "external_id": external_id,
             "observed_at": observed_at, "payload": source_payload},
            actor,
            role,
            decide,
            expected_version,
        )
        result = self.get_item(item_id)
        result["source_result"] = {
            "source_id": source["id"],
            "state": source["state"],
            "observed_at": source["observed_at"],
            "becomes_current": decision.get("becomes_current", False),
            "basis_version": decision.get("new_basis_version"),
            "blocked_action": bool(decision.get("blocked")),
        }
        return result

    def confirm_source(self, item_id, source_id, actor, role, expected_version=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CONFIRM_SOURCE_ROLES:
            raise DomainError("forbidden", "只有协调员可以确认待确认来源", 403)
        if expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        item = self.repository.get_item(item_id)
        if item["status"] not in {"pending", "assessed", "coordinating", "executing"}:
            raise DomainError("invalid_state", "当前状态 %s 不允许确认来源" % item["status"])

        def decide(current, existing_sources, sid):
            return rules.confirm_decision(current, existing_sources, sid, item["status"])

        source, decision = self.repository.confirm_source(
            item_id, source_id, actor, role, decide, expected_version
        )
        result = self.get_item(item_id)
        result["source_result"] = {
            "source_id": source["id"],
            "state": source["state"],
            "becomes_current": True,
            "basis_version": decision.get("new_basis_version"),
            "blocked_action": bool(decision.get("blocked")),
        }
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

        new_status, new_payload, event_payload, extra_events = rules.apply_action(
            item, action, payload, actor, role
        )

        side_effect = None
        if action == "approve":
            side_effect = self._approval_side_effect
        item, side_result = self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload,
            expected_version, extra_events=extra_events, side_effect=side_effect,
        )
        result = self.get_item(item_id)
        if side_result is not None:
            result["notification_plan"] = side_result
        return result

    def _approval_side_effect(self, conn, item_id, new_payload, new_status, version, actor, role):
        approved = new_payload.get("approved_maneuver")
        if not approved:
            return None
        return self.repository.create_notification_plan(conn, item_id, new_payload, approved)

    def run_dispatch(self, plan_id, actor, role):
        """派发一个通知计划：本轮只领取并发送一次未完成步骤；已发送步骤永不重复。
        每条步骤独立事务、独立成败，崩溃后再次调用即接着原进度继续，失败步骤在后续轮次重试。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.DISPATCH_ROLES:
            raise DomainError("forbidden", "当前角色不能派发通知", 403)
        plan = self.repository.get_plan(plan_id)
        summary = {"plan_id": plan_id, "status": plan["status"], "sent": [], "retried": [], "failed": [], "skipped": []}
        if plan["status"] == "superseded":
            summary["skipped"].append("plan_superseded")
            return summary
        if plan["status"] in ("completed", "completed_partial"):
            return summary

        run_token = uuid.uuid4().hex
        while True:
            step = self.repository.claim_next_step(plan_id, run_token)
            if step is None:
                break
            try:
                delivery = self.notifier.send(
                    step["recipient"],
                    step["channel"],
                    step["subject"],
                    step["body"],
                    # 重试携带同一幂等键：完成的指令不会被重复下发。
                    step["idempotency_key"],
                )
                status = self.repository.mark_step_sent(step["id"])
                if step["attempts"] > 1:
                    summary["retried"].append(step["step_index"])
                summary["sent"].append({"step_index": step["step_index"], "recipient": step["recipient"],
                                        "idempotency_key": step["idempotency_key"], "delivery": delivery})
            except Exception as exc:  # noqa: BLE001 - 单步失败不影响其他步骤
                self.repository.mark_step_failed(step["id"], exc)
                summary["failed"].append({"step_index": step["step_index"], "recipient": step["recipient"],
                                          "error": str(exc)[:500]})
                # 通知发送失败后只重试未完成步骤：本轮继续派发后续步骤，
                # 稍后重跑本计划时再重试这条失败步骤（它仍是唯一未完成的步骤）。
                continue
        plan = self.repository.get_plan(plan_id)
        summary["status"] = plan["status"]
        return summary

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources_public(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        basis = rules.current_basis(item["payload"])
        item["assessment"]["basis_version"] = basis["basis_version"]
        item["assessment"]["basis_source_id"] = basis.get("basis_source_id")
        item["assessment"]["observed_at"] = basis.get("observed_at")
        item["current_basis"] = basis
        item["payload"]["current_basis"] = basis
        item["blocked_actions"] = item["payload"].get("blocked_actions", [])
        item["notification_plans"] = self.repository.list_plans(item_id)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
