from datetime import datetime, timezone

from .domain import DomainError, NotFoundError

ENTITY_TYPE = "space_conjunction"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst"}
SOURCE_ROLES = {"analyst", "operator"}
CONFIRM_SOURCE_ROLES = {"coordinator"}
DISPATCH_ROLES = {"coordinator", "operator"}
ACTION_ROLES = {
    "assess": {"analyst"},
    "record_opinion": {"operator"},
    "approve": {"coordinator"},
    "execute": {"operator"},
    "resolve": {"coordinator"},
    "cancel": {"coordinator"},
    "report_revision": {"analyst"},
    "confirm_source": {"coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {
    "approve",
    "execute",
    "resolve",
    "cancel",
    "confirm_source",
}


def assess(payload):
    ratio = float(payload.get("miss_distance_m", 0)) / max(float(payload.get("covariance_m", 1)), 1.0)
    tca_hours = float(payload.get("hours_to_tca", 24))
    severity = max(0.0, 100.0 - min(95.0, ratio * 20.0))
    urgency = max(0.0, min(20.0, (24.0 - tca_hours) * 0.8))
    score = round(min(100.0, severity + urgency), 2)
    if score >= 80:
        level = "high"
    elif score >= 50:
        level = "medium"
    else:
        level = "low"
    return {"score": score, "level": level, "distance_to_covariance_ratio": round(ratio, 3)}


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _require_number(payload, name, minimum=None):
    try:
        value = float(payload[name])
    except (KeyError, TypeError, ValueError):
        raise DomainError("field_required", "%s 不能为空" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def _require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def current_basis(current):
    """当前评估依据。创建事件时录入的轨道参数是第 0 版依据；来源记录从 1 开始。"""
    basis = current.get("current_basis")
    if basis:
        return basis
    return {
        "basis_version": 0,
        "basis_source_id": None,
        "source_type": "create",
        "external_id": None,
        "observed_at": None,
    }


def _snapshot_basis(basis):
    return {
        "basis_version": basis["basis_version"],
        "basis_source_id": basis.get("basis_source_id"),
        "source_type": basis.get("source_type"),
        "external_id": basis.get("external_id"),
        "observed_at": basis.get("observed_at"),
    }


def _annotate_assessment(result, current):
    basis = current_basis(current)
    result = dict(result)
    result["basis_version"] = basis["basis_version"]
    result["basis_source_id"] = basis.get("basis_source_id")
    result["observed_at"] = basis.get("observed_at")
    return result


def _switch_basis(current, new_basis, distance, covariance, status, at=None):
    """切到一条新的当前依据：重算评估；尚未执行的批准失效并记入受阻动作；
    已发出的执行保留其冻结的原依据。返回 (失效的批准或 None, 附加审计事件列表, 新状态)。
    批准失效意味着回到 assessed 阶段重新评估/批准；executing/resolved 不回退。"""
    current["current_basis"] = _snapshot_basis(new_basis)
    current["miss_distance_m"] = distance
    current["covariance_m"] = covariance
    current["assessment"] = _annotate_assessment(assess(current), current)

    blocked = None
    extra_events = []
    approved = current.get("approved_maneuver")
    # 只在协调中（已批准、指令尚未发出）时使批准失效；executing/resolved 的执行已冻结原依据。
    if approved and "command_ref" not in current:
        blocked = {
            "action": "approve",
            "approved_maneuver": dict(approved),
            "basis_version": approved.get("basis_version"),
            "basis_source_id": approved.get("basis_source_id"),
            "observed_at": approved.get("observed_at"),
            "invalidated_at": at or now_iso(),
            "new_basis_version": new_basis["basis_version"],
            "new_basis_source_id": new_basis.get("basis_source_id"),
        }
        current.setdefault("blocked_actions", []).append(blocked)
        current.pop("approved_maneuver", None)
        extra_events.append({"event_type": "approval_invalidated", "payload": blocked})
        status = "assessed"
    return blocked, extra_events, status


def source_decision(current, existing_sources, new_source, new_source_id, current_status=None, at=None):
    """按观测时刻选择当前依据。

    - 观测时刻新于当前依据：成为当前依据，原当前依据降为历史。
    - 观测时刻旧于当前依据：只留历史，评估与协调动作不变。
    - 观测时刻相同（两个站同时提交）：先提交的形成当前依据，后到的待确认，
      必须由协调员显式确认才会切换依据。
    返回仓库落库所需的决策字典。
    """
    new_observed = parse_iso(new_source["observed_at"])
    basis = current_basis(current)
    basis_observed = parse_iso(basis["observed_at"]) if basis.get("observed_at") else None

    decision = {
        "new_state": "history",
        "becomes_current": False,
        "old_current_source_id": None,
        "blocked": None,
        "new_basis_version": None,
        "event_type": "source_recorded",
        "event_payload": {
            "source_id": new_source_id,
            "source_type": new_source["source_type"],
            "external_id": new_source["external_id"],
            "state": "history",
            "observed_at": new_source["observed_at"],
        },
        "extra_events": [],
        "next_payload": None,
    }

    newer = basis_observed is None or new_observed > basis_observed
    same = basis_observed is not None and new_observed == basis_observed
    has_pending = any(source.get("state") == "pending" for source in existing_sources)

    if same:
        # 两个站在同一观测时刻提交：只形成一份当前依据，后到的待确认，
        # 必须由协调员显式确认才会切换依据。
        decision["new_state"] = "pending"
        decision["event_type"] = "source_pending"
        decision["event_payload"]["state"] = "pending"
        return decision

    if newer and has_pending:
        # 上一轮同时刻来源仍待确认时，更新的观测也不能静默越过争议，
        # 一并排队等待确认；确认后其余待确认/更旧记录只留历史。
        decision["new_state"] = "pending"
        decision["event_type"] = "source_pending"
        decision["event_payload"]["state"] = "pending"
        return decision

    if newer:
        next_payload = dict(current)
        new_basis = {
            "basis_version": int(basis["basis_version"]) + 1,
            "basis_source_id": new_source_id,
            "source_type": new_source["source_type"],
            "external_id": new_source["external_id"],
            "observed_at": new_source["observed_at"],
        }
        blocked, extra_events, new_status = _switch_basis(
            next_payload,
            new_basis,
            new_source["miss_distance_m"],
            new_source["covariance_m"],
            current_status,
            at=at,
        )
        decision.update(
            {
                "new_state": "current",
                "becomes_current": True,
                "old_current_source_id": basis.get("basis_source_id"),
                "new_basis_version": new_basis["basis_version"],
                "blocked": blocked,
                "new_status": new_status,
                "event_type": "source_recorded",
                "extra_events": extra_events,
                "next_payload": next_payload,
            }
        )
        decision["event_payload"].update(
            {
                "state": "current",
                "basis_version": new_basis["basis_version"],
                "previous_basis_version": basis["basis_version"],
                "previous_source_id": basis.get("basis_source_id"),
                "blocked_action": bool(blocked),
            }
        )
        return decision

    # 晚到的旧记录：只留历史。
    return decision


def confirm_decision(current, existing_sources, source_id, current_status=None, at=None):
    """协调员确认一条待确认来源：确认后它成为唯一当前依据。"""
    target = None
    for source in existing_sources:
        if int(source["id"]) == int(source_id):
            target = source
            break
    if target is None:
        raise NotFoundError("source_not_found", "来源记录不存在")
    if target.get("state") != "pending":
        raise DomainError("source_not_pending", "只有待确认的来源记录可以确认", 409)

    basis = current_basis(current)
    next_payload = dict(current)
    new_basis = {
        "basis_version": int(basis["basis_version"]) + 1,
        "basis_source_id": int(source_id),
        "source_type": target["source_type"],
        "external_id": target["external_id"],
        "observed_at": target["observed_at"],
    }
    source_payload = target["payload"] if isinstance(target.get("payload"), dict) else {}
    blocked, extra_events, new_status = _switch_basis(
        next_payload,
        new_basis,
        source_payload.get("miss_distance_m", next_payload.get("miss_distance_m")),
        source_payload.get("covariance_m", next_payload.get("covariance_m")),
        current_status,
        at=at,
    )
    return {
        "new_state": "current",
        "becomes_current": True,
        "old_current_source_id": basis.get("basis_source_id"),
        "blocked": blocked,
        "new_basis_version": new_basis["basis_version"],
        "new_status": new_status,
        "event_payload": {
            "source_id": int(source_id),
            "state": "current",
            "basis_version": new_basis["basis_version"],
            "previous_basis_version": basis["basis_version"],
            "previous_source_id": basis.get("basis_source_id"),
            "blocked_action": bool(blocked),
        },
        "extra_events": [
            {"event_type": "approval_invalidated", "payload": blocked}
        ] if blocked else [],
        "next_payload": next_payload,
    }


def apply_action(item, action, payload, actor, role, at=None):
    status = item["status"]
    current = dict(item["payload"])
    at = at or now_iso()

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        age = float(current.get("track_age_hours", 0))
        if age > 6:
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        result = _annotate_assessment(assess(current), current)
        current["assessment"] = result
        basis = current_basis(current)
        return "assessed", current, {"assessment": result, "actor": actor, "basis_version": basis["basis_version"]}, []

    if action == "report_revision":
        _need_status(item, {"pending", "assessed", "coordinating", "executing"})
        revision = {
            "observed_at": _require_text(payload, "observed_at"),
            "miss_distance_m": _require_number(payload, "miss_distance_m", 0),
            "covariance_m": _require_number(payload, "covariance_m", 0.001),
            "source": _require_text(payload, "source"),
        }
        if revision["covariance_m"] <= 0:
            raise DomainError("invalid_covariance", "协方差必须大于零")
        current.setdefault("revisions", []).append(revision)
        basis = current_basis(current)
        new_basis = {
            "basis_version": int(basis["basis_version"]) + 1,
            "basis_source_id": basis.get("basis_source_id"),
            "source_type": "revision",
            "external_id": revision["source"],
            "observed_at": revision["observed_at"],
        }
        blocked, extra_events, next_status = _switch_basis(
            current, new_basis, revision["miss_distance_m"], revision["covariance_m"], status, at=at
        )
        event = {
            "revision": revision,
            "basis_version": new_basis["basis_version"],
            "blocked_action": bool(blocked),
        }
        return next_status, current, event, extra_events

    if action == "record_opinion":
        _need_status(item, {"assessed", "coordinating"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in {"approve", "reject", "request_review"}:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        entry = {"operator": operator, "opinion": opinion, "reason": payload.get("reason", "")}
        current.setdefault("opinions", []).append(entry)
        if opinion in {"reject", "request_review"}:
            current["conflict"] = True
        return status, current, {"opinion": entry}, []

    if action == "approve":
        _need_status(item, {"assessed"})
        if current.get("conflict"):
            raise DomainError("unresolved_conflict", "存在未解决的运营方冲突意见", 409)
        fuel = _require_number(payload, "fuel_cost_m_s", 0)
        budget = float(current.get("fuel_budget_m_s", 0))
        if fuel > budget:
            raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
        window = _require_text(payload, "maneuver_window")
        basis = current_basis(current)
        # 批准冻结其依据的快照；后续依据变化不会改写它。
        current["approved_maneuver"] = {
            "fuel_cost_m_s": fuel,
            "maneuver_window": window,
            "basis_version": basis["basis_version"],
            "basis_source_id": basis.get("basis_source_id"),
            "observed_at": basis.get("observed_at"),
            "approved_at": at,
        }
        return "coordinating", current, {"approved_maneuver": current["approved_maneuver"]}, []

    if action == "execute":
        _need_status(item, {"coordinating"})
        approved = current.get("approved_maneuver")
        if not approved:
            raise DomainError("invalid_state", "没有已批准的规避动作可执行")
        command_ref = _require_text(payload, "command_ref")
        # 指令发出时冻结所执行批准的原依据，之后依据变化不影响这条执行。
        current["command_ref"] = command_ref
        current["executed_basis"] = {
            "basis_version": approved.get("basis_version"),
            "basis_source_id": approved.get("basis_source_id"),
            "observed_at": approved.get("observed_at"),
            "executed_at": at,
        }
        return "executing", current, {
            "command_ref": command_ref,
            "basis_version": approved.get("basis_version"),
            "basis_source_id": approved.get("basis_source_id"),
        }, []

    if action == "resolve":
        _need_status(item, {"executing"})
        report_ref = _require_text(payload, "report_ref")
        current["resolution"] = {"report_ref": report_ref, "resolved_by": actor}
        return "resolved", current, {"report_ref": report_ref}, []

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _require_text(payload, "reason")
        current["cancellation"] = {"reason": reason, "cancelled_by": actor}
        return "cancelled", current, {"reason": reason}, []

    raise DomainError("unknown_action", "不支持的操作")
