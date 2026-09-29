"""通知派发：每一步独立持久化，失败只重试未完成步骤。

发送方通过幂等键（idempotency_key）去重：即使进程在“已发送、未落库”之间崩溃，
重试点也携带相同键，接收侧不会重复下发同一条指令。
"""


class NotificationError(Exception):
    pass


class LoggingNotifier:
    """默认通知器：演示用，始终成功。生产环境替换为真实通道适配器。"""

    def __init__(self, sink=None):
        self.sink = sink

    def send(self, recipient, channel, subject, body, idempotency_key):
        line = "[notify %s/%s key=%s] %s | %s" % (channel, recipient, idempotency_key, subject, body)
        if self.sink is not None:
            self.sink.append(line)
        return {"recipient": recipient, "channel": channel, "idempotency_key": idempotency_key}


def plan_recipients(payload):
    """一份批准对应的通知目标：事件涉及的各运营方各一步。"""
    return [str(org).strip() for org in payload.get("operating_organizations", []) if str(org).strip()]


def build_steps(item_id, plan_id, payload, approved_maneuver):
    recipients = plan_recipients(payload)
    window = approved_maneuver.get("maneuver_window", "")
    subject = "规避动作已批准 basis=%s" % approved_maneuver.get("basis_version")
    body = "item=%s 燃料预算=%s m/s 机动窗口=%s" % (
        item_id,
        approved_maneuver.get("fuel_cost_m_s"),
        window,
    )
    steps = []
    for index, recipient in enumerate(recipients):
        steps.append(
            {
                "step_index": index,
                "recipient": recipient,
                "channel": "operator_notify",
                "subject": subject,
                "body": body,
                "idempotency_key": "notify:%d:%d" % (plan_id, index),
            }
        )
    return steps


# 仍需继续派发的步骤状态；sending 是“发送中崩溃”的恢复状态，靠幂等键安全重试。
RETRYABLE_STATES = ("pending", "failed", "sending")
DONE_STATES = ("sent",)
