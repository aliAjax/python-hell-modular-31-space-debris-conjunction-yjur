from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository, sender=None):
        self.repository = repository
        self.sender = sender or self._default_sender

    def _default_sender(self, step):
        # the demo has no real notification channel, so sending succeeds
        return

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

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
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        if action in ("approve", "execute"):
            self._trigger_notifications(item_id, action, new_payload)
        return self.get_item(item_id)

    def _notification_steps(self, action, payload):
        if action == "approve":
            orgs = payload.get("operating_organizations", [])
            maneuver = payload.get("approved_maneuver")
            if orgs:
                return [
                    {
                        "step_name": "notify_operator:%s" % org,
                        "payload": {"org": org, "maneuver": maneuver},
                    }
                    for org in orgs
                ]
            return [{"step_name": "notify_operators", "payload": {"maneuver": maneuver}}]
        if action == "execute":
            return [
                {"step_name": "send_command", "payload": {"command_ref": payload.get("command_ref")}},
                {"step_name": "notify_execution", "payload": {"command_ref": payload.get("command_ref")}},
            ]
        return []

    def _trigger_notifications(self, item_id, action, payload):
        steps = self._notification_steps(action, payload)
        if not steps:
            return {"total": 0, "sent": 0, "failed": 0, "pending": 0}
        self.repository.create_notification_steps(item_id, action, steps)
        return self._process_notifications(item_id, action)

    def _process_notifications(self, item_id, trigger_action=None):
        if trigger_action:
            steps = self.repository.list_notification_steps(item_id, trigger_action)
        else:
            steps = self.repository.pending_notification_steps(item_id)
        sent = failed = pending = 0
        for step in steps:
            if step["status"] == "sent":
                sent += 1
                continue
            try:
                self.sender(step)
                self.repository.mark_notification_sent(step["id"])
                sent += 1
            except Exception as exc:
                self.repository.mark_notification_failed(step["id"], str(exc))
                failed += 1
        total = len(steps)
        pending = total - sent - failed
        return {"total": total, "sent": sent, "failed": failed, "pending": pending}

    def retry_notifications(self, item_id, trigger_action=None):
        return self._process_notifications(item_id, trigger_action)

    def list_notifications(self, item_id):
        return self.repository.list_notification_steps(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["notifications"] = self.repository.list_notification_steps(item_id)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
