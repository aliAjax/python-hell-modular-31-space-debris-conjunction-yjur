import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src import notifications
from src.domain import ConflictError, DomainError, NotFoundError


CREATE_PAYLOAD = {
    "primary_object_id": "SAT-2",
    "secondary_object_id": "DEB-3",
    "tca": "2026-09-29T12:00:00+00:00",
    "miss_distance_m": 120,
    "covariance_m": 100,
    "fuel_budget_m_s": 4,
    "track_age_hours": 1,
    "operating_organizations": ["Org-A", "Org-B"],
}


class BasisChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _assessed_item(self, miss=120):
        payload = dict(CREATE_PAYLOAD, miss_distance_m=miss)
        item = self.service.create_item(payload, "a", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "a", "analyst", item["version"])
        return item

    def test_late_old_source_is_history_only(self):
        item = self._assessed_item(miss=120)
        # 更新的来源成为当前依据
        newer = self.service.add_source(item["id"], {
            "source_type": "radar", "external_id": "ST-A",
            "observed_at": "2026-09-29T06:00:00+00:00",
            "miss_distance_m": 80, "covariance_m": 100,
        }, "a", "analyst", expected_version=item["version"])
        self.assertEqual(newer["payload"]["current_basis"]["basis_version"], 1)
        self.assertEqual(newer["payload"]["miss_distance_m"], 80)

        # 晚到的旧观测只留历史，评估数值不变
        late = self.service.add_source(item["id"], {
            "source_type": "radar", "external_id": "ST-OLD",
            "observed_at": "2026-09-29T04:00:00+00:00",
            "miss_distance_m": 5000, "covariance_m": 100,
        }, "a", "analyst", expected_version=newer["version"])
        self.assertEqual(late["source_result"]["state"], "history")
        self.assertFalse(late["source_result"]["becomes_current"])
        self.assertEqual(late["payload"]["miss_distance_m"], 80)
        self.assertEqual(late["payload"]["current_basis"]["basis_version"], 1)
        states = {s["external_id"]: s["state"] for s in late["sources"]}
        self.assertEqual(states["ST-A"], "current")
        self.assertEqual(states["ST-OLD"], "history")

    def test_assessment_and_maneuver_share_version_chain(self):
        item = self._assessed_item(miss=120)
        v0 = item["payload"]["assessment"]["basis_version"]
        self.assertEqual(v0, 0)
        newer = self.service.add_source(item["id"], {
            "source_type": "radar", "external_id": "ST-A",
            "observed_at": "2026-09-29T06:00:00+00:00",
            "miss_distance_m": 80, "covariance_m": 100,
        }, "a", "analyst", expected_version=item["version"])
        # 依据变化后自动重算的评估挂在同一版本链的新版本上
        self.assertEqual(newer["payload"]["assessment"]["basis_version"], 1)
        self.assertEqual(newer["payload"]["assessment"]["basis_source_id"],
                         newer["source_result"]["source_id"])

    def test_same_observation_time_one_current_one_pending(self):
        item = self._assessed_item()
        first = self.service.add_source(item["id"], {
            "source_type": "radar", "external_id": "ST-A",
            "observed_at": "2026-09-29T06:00:00+00:00",
            "miss_distance_m": 80, "covariance_m": 100,
        }, "a", "analyst", expected_version=item["version"])
        second = self.service.add_source(item["id"], {
            "source_type": "radar", "external_id": "ST-B",
            "observed_at": "2026-09-29T06:00:00+00:00",
            "miss_distance_m": 200, "covariance_m": 100,
        }, "a", "analyst", expected_version=first["version"])
        self.assertEqual(second["source_result"]["state"], "pending")
        self.assertEqual(second["payload"]["miss_distance_m"], 80)
        states = {s["external_id"]: s["state"] for s in second["sources"]}
        self.assertEqual(states["ST-A"], "current")
        self.assertEqual(states["ST-B"], "pending")

        # 非协调员不能确认
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_source(item["id"], second["source_result"]["source_id"],
                                        "a", "analyst", second["version"])
        self.assertEqual(ctx.exception.status, 403)

        # 协调员确认后，ST-B 成为当前依据，ST-A 降为历史
        confirmed = self.service.confirm_source(
            item["id"], second["source_result"]["source_id"], "c", "coordinator", second["version"])
        states = {s["external_id"]: s["state"] for s in confirmed["sources"]}
        self.assertEqual(states["ST-B"], "current")
        self.assertEqual(states["ST-A"], "history")
        self.assertEqual(confirmed["payload"]["miss_distance_m"], 200)

    def test_concurrent_simultaneous_submissions_single_current(self):
        item = self._assessed_item()
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def submit(external_id):
            try:
                barrier.wait(timeout=10)
                res = self.service.add_source(item["id"], {
                    "source_type": "radar", "external_id": external_id,
                    "observed_at": "2026-09-29T06:00:00+00:00",
                    "miss_distance_m": 80, "covariance_m": 100,
                }, "a", "analyst")  # 不带 expected_version，模拟两站同时提交
                results.append(res["source_result"]["state"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=submit, args=("ST-A",))
        t2 = threading.Thread(target=submit, args=("ST-B",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertFalse(errors, errors)
        self.assertEqual(sorted(results), ["current", "pending"])
        refreshed = self.service.get_item(item["id"])
        states = sorted(s["state"] for s in refreshed["sources"])
        self.assertEqual(states, ["current", "pending"])

    def test_unexecuted_approval_invalidated_and_listed(self):
        item = self._assessed_item(miss=120)
        approved = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.0, "maneuver_window": "w1",
        }, "c", "coordinator", item["version"])
        self.assertEqual(approved["status"], "coordinating")
        plan_id = approved["notification_plan"]["plan_id"]

        # 依据更新：尚未执行的批准失效
        changed = self.service.add_source(approved["id"], {
            "source_type": "radar", "external_id": "ST-A",
            "observed_at": "2026-09-29T06:00:00+00:00",
            "miss_distance_m": 60, "covariance_m": 100,
        }, "a", "analyst", expected_version=approved["version"])
        self.assertEqual(changed["status"], "assessed")
        self.assertNotIn("approved_maneuver", changed["payload"])
        blocked = changed["payload"]["blocked_actions"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["action"], "approve")
        self.assertEqual(blocked[0]["basis_version"], 0)
        self.assertEqual(blocked[0]["new_basis_version"], 1)
        # 未发完的通知计划随批准一并作废
        self.assertEqual(changed["notification_plans"][0]["status"], "superseded")

        # 失效后可以基于新依据重新评估、重新批准
        re_item = self.service.act(changed["id"], "assess", {"hours_to_tca": 10},
                                   "a", "analyst", changed["version"])
        re_approved = self.service.act(re_item["id"], "approve", {
            "fuel_cost_m_s": 1.0, "maneuver_window": "w2",
        }, "c", "coordinator", re_item["version"])
        self.assertEqual(re_approved["status"], "coordinating")
        self.assertEqual(re_approved["payload"]["approved_maneuver"]["basis_version"], 1)

    def test_executed_maneuver_keeps_original_basis(self):
        item = self._assessed_item(miss=120)
        approved = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.0, "maneuver_window": "w1",
        }, "c", "coordinator", item["version"])
        executed = self.service.act(approved["id"], "execute", {"command_ref": "CMD-7"},
                                    "op", "operator", approved["version"])
        self.assertEqual(executed["status"], "executing")
        self.assertEqual(executed["payload"]["executed_basis"]["basis_version"], 0)
        self.assertEqual(executed["payload"]["command_ref"], "CMD-7")

        # 执行之后依据再变化：已发出的指令保留原依据，状态不回退
        changed = self.service.add_source(executed["id"], {
            "source_type": "radar", "external_id": "ST-A",
            "observed_at": "2026-09-29T06:00:00+00:00",
            "miss_distance_m": 60, "covariance_m": 100,
        }, "a", "analyst", expected_version=executed["version"])
        self.assertEqual(changed["status"], "executing")
        self.assertEqual(changed["payload"]["command_ref"], "CMD-7")
        self.assertEqual(changed["payload"]["executed_basis"]["basis_version"], 0)
        self.assertEqual(changed["payload"]["blocked_actions"], [])
        # 当前评估已经是新依据
        self.assertEqual(changed["payload"]["current_basis"]["basis_version"], 1)

    def test_confirm_non_pending_source_rejected(self):
        item = self._assessed_item()
        added = self.service.add_source(item["id"], {
            "source_type": "radar", "external_id": "ST-A",
            "observed_at": "2026-09-29T06:00:00+00:00",
            "miss_distance_m": 80, "covariance_m": 100,
        }, "a", "analyst", expected_version=item["version"])
        sid = added["source_result"]["source_id"]
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_source(item["id"], sid, "c", "coordinator", added["version"])
        self.assertEqual(ctx.exception.code, "source_not_pending")

    def test_confirm_missing_source_404(self):
        item = self._assessed_item()
        with self.assertRaises(NotFoundError):
            self.service.confirm_source(item["id"], 999, "c", "coordinator", item["version"])


if __name__ == "__main__":
    unittest.main()
