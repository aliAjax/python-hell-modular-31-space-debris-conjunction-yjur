import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


CREATE_PAYLOAD = {
    "primary_object_id": "SAT-4",
    "secondary_object_id": "DEB-5",
    "tca": "2026-09-30T12:00:00+00:00",
    "miss_distance_m": 80,
    "covariance_m": 100,
    "fuel_budget_m_s": 4,
    "track_age_hours": 1,
    "operating_organizations": ["Org-A", "Org-B", "Org-C"],
}


class FlakyNotifier:
    """按幂等键记录下发次数；可对指定接收者失败，且可在崩溃模拟时验证“只发一次”。"""

    def __init__(self, fail_for=(), crash_after_send=()):
        self.fail_for = set(fail_for)
        self.crash_after_send = set(crash_after_send)
        self.crashed = False
        self.sent = []
        self.keys_seen = {}

    def send(self, recipient, channel, subject, body, idempotency_key):
        if self.crashed:
            # 进程崩溃后，本轮后续步骤不会再执行；只有重启后的新实例才会继续。
            raise RuntimeError("process is dead")
        self.keys_seen[idempotency_key] = self.keys_seen.get(idempotency_key, 0) + 1
        if recipient in self.crash_after_send:
            # 模拟“已发送但落库前进程崩溃”：通知实际已外发
            self.sent.append((recipient, idempotency_key))
            self.crashed = True
            raise RuntimeError("process crashed after delivery")
        if recipient in self.fail_for:
            raise RuntimeError("network down")
        self.sent.append((recipient, idempotency_key))
        return {"ok": True, "recipient": recipient, "key": idempotency_key}


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.notifier = FlakyNotifier()
        self.service = Service(self.repo, self.notifier)
        item = self.service.create_item(CREATE_PAYLOAD, "a", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "a", "analyst", item["version"])
        approved = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.0, "maneuver_window": "w1",
        }, "c", "coordinator", item["version"])
        self.item_id = approved["id"]
        self.plan_id = approved["notification_plan"]["plan_id"]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_full_dispatch_is_idempotent_on_retry(self):
        summary = self.service.run_dispatch(self.plan_id, "c", "coordinator")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(len(summary["sent"]), 3)
        self.assertEqual([s["recipient"] for s in summary["sent"]], ["Org-A", "Org-B", "Org-C"])
        # 每个步骤的幂等键唯一且稳定
        keys = [s["idempotency_key"] for s in summary["sent"]]
        self.assertEqual(len(set(keys)), 3)

        # 再次派发：完成的指令不重复下发
        again = self.service.run_dispatch(self.plan_id, "c", "coordinator")
        self.assertEqual(again["sent"], [])
        self.assertEqual(len(self.notifier.sent), 3)

    def test_failure_retries_only_incomplete_steps(self):
        self.notifier.fail_for = {"Org-B"}
        first = self.service.run_dispatch(self.plan_id, "c", "coordinator")
        self.assertEqual(first["status"], "failed")
        self.assertEqual(len(first["failed"]), 1)
        self.assertEqual(first["failed"][0]["recipient"], "Org-B")
        # Org-A、Org-C 已完成，绝不重复
        self.assertEqual(sorted(recipient for recipient, _ in self.notifier.sent), ["Org-A", "Org-C"])

        # 恢复通道后重试：只下发未完成的 Org-B
        self.notifier.fail_for = set()
        second = self.service.run_dispatch(self.plan_id, "c", "coordinator")
        self.assertEqual(second["status"], "completed")
        self.assertEqual([s["recipient"] for s in second["sent"]], ["Org-B"])
        self.assertEqual(sorted(recipient for recipient, _ in self.notifier.sent),
                         ["Org-A", "Org-B", "Org-C"])
        # Org-B 的尝试次数被记录（失败一次 + 成功一次）
        conn = self.repo.connect()
        try:
            row = conn.execute(
                "SELECT attempts FROM notification_steps WHERE plan_id=? AND recipient='Org-B'",
                (self.plan_id,),
            ).fetchone()
            self.assertEqual(row["attempts"], 2)
        finally:
            conn.close()

    def test_restart_after_crash_resumes_from_progress(self):
        # 第一步外发成功，但落库前崩溃（发送侧已收到，携带幂等键）
        self.notifier.crash_after_send = {"Org-A"}
        crashed = self.service.run_dispatch(self.plan_id, "c", "coordinator")
        self.assertEqual(crashed["status"], "failed")
        self.assertEqual(len(crashed["sent"]), 0)
        # Org-A 实际已外发，Org-B/C 未外发
        self.assertEqual([r for r, _ in self.notifier.sent], ["Org-A"])

        # 重启（全新 service 实例、同一数据库）：接着原进度继续
        service2 = Service(self.repo, FlakyNotifier())
        resumed = service2.run_dispatch(self.plan_id, "c", "coordinator")
        self.assertEqual(resumed["status"], "completed")
        # 崩溃的步骤处于 sending，按同一幂等键安全重试；后两步正常下发
        recipients = [s["recipient"] for s in resumed["sent"]]
        self.assertIn("Org-A", recipients)
        self.assertIn("Org-B", recipients)
        self.assertIn("Org-C", recipients)
        # 两个 notifier 各自的下发记录：Org-A 只在第二次重试时补一次成功记录
        self.assertEqual([r for r, _ in service2.notifier.sent].count("Org-A"), 1)

    def test_superseded_plan_is_not_dispatched(self):
        # 依据变化使批准失效，通知计划作废
        self.service.add_source(self.item_id, {
            "source_type": "radar", "external_id": "ST-A",
            "observed_at": "2026-09-30T06:00:00+00:00",
            "miss_distance_m": 40, "covariance_m": 100,
        }, "a", "analyst", expected_version=self.service.get_item(self.item_id)["version"])
        summary = self.service.run_dispatch(self.plan_id, "c", "coordinator")
        self.assertEqual(summary["status"], "superseded")
        self.assertEqual(self.notifier.sent, [])

    def test_dispatch_requires_permission(self):
        from src.domain import DomainError
        with self.assertRaises(DomainError) as ctx:
            self.service.run_dispatch(self.plan_id, "a", "analyst")
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
