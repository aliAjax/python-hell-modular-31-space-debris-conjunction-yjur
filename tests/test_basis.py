import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class RecordingSender:
    def __init__(self, fail_step=None):
        self.fail_step = fail_step
        self.calls = []

    def __call__(self, step):
        self.calls.append(step["step_name"])
        if self.fail_step is not None and step["step_name"] == self.fail_step:
            raise Exception("simulated notification failure")


class BasisChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "primary_object_id": "SAT-1",
            "secondary_object_id": "DEB-9",
            "tca": "2026-09-29T12:00:00+00:00",
            "miss_distance_m": 120,
            "covariance_m": 100,
            "fuel_budget_m_s": 5,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A", "Org-B"],
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create_and_assess(self):
        item = self.service.create_item(self.payload, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        return item

    def _source(self, observed_at, miss, cov=100, source_type="radar", external_id=None):
        return {
            "source_type": source_type,
            "external_id": external_id or ("SRC-%s" % observed_at),
            "observed_at": observed_at,
            "miss_distance_m": miss,
            "covariance_m": cov,
        }

    def test_current_basis_selected_by_observation_time(self):
        item = self._create_and_assess()
        # first source becomes the current basis
        r1 = self.service.add_source(item["id"], self._source("2026-09-29T10:00:00Z", 50), "analyst-1", "analyst")
        self.assertTrue(r1["basis_changed"])
        self.assertEqual(r1["source"]["status"], "current")
        # a late source with an older observation time stays in history
        r2 = self.service.add_source(item["id"], self._source("2026-09-29T09:00:00Z", 200), "analyst-1", "analyst")
        self.assertFalse(r2["basis_changed"])
        self.assertEqual(r2["source"]["status"], "history")
        # a source with a later observation time becomes the new current basis
        r3 = self.service.add_source(item["id"], self._source("2026-09-29T11:00:00Z", 30), "analyst-1", "analyst")
        self.assertTrue(r3["basis_changed"])
        self.assertEqual(r3["source"]["status"], "current")
        # the old current basis is now history
        sources = self.service.list_items()[0]
        item_full = self.service.get_item(item["id"])
        statuses = {s["observed_at"]: s["status"] for s in item_full["sources"]}
        self.assertEqual(statuses["2026-09-29T10:00:00Z"], "history")
        self.assertEqual(statuses["2026-09-29T09:00:00Z"], "history")
        self.assertEqual(statuses["2026-09-29T11:00:00Z"], "current")
        # the current basis reflects the latest observation, not the last arrival
        self.assertEqual(item_full["payload"]["miss_distance_m"], 30)
        self.assertEqual(item_full["current_basis"]["miss_distance_m"] if "current_basis" in item_full else item_full["payload"]["current_basis"]["miss_distance_m"], 30)

    def test_tied_observation_time_keeps_one_current_one_pending(self):
        item = self._create_and_assess()
        r1 = self.service.add_source(item["id"], self._source("2026-09-29T10:00:00Z", 50, external_id="A"), "analyst-1", "analyst")
        self.assertEqual(r1["source"]["status"], "current")
        # a second source with the same observation time cannot be decided
        r2 = self.service.add_source(item["id"], self._source("2026-09-29T10:00:00Z", 60, external_id="B"), "analyst-2", "analyst")
        self.assertFalse(r2["basis_changed"])
        self.assertEqual(r2["source"]["status"], "pending")
        item_full = self.service.get_item(item["id"])
        statuses = {s["external_id"]: s["status"] for s in item_full["sources"]}
        self.assertEqual(statuses["A"], "current")
        self.assertEqual(statuses["B"], "pending")
        # the pending source does not drive the assessment
        self.assertEqual(item_full["payload"]["miss_distance_m"], 50)

    def test_basis_change_invalidates_unexecuted_approval_and_lists_blocked(self):
        item = self._create_and_assess()
        # establish a source basis before approving
        self.service.add_source(item["id"], self._source("2026-09-29T10:00:00Z", 50), "analyst-1", "analyst")
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2.5, "maneuver_window": "w"}, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        # a later observation changes the basis; the unexecuted approval is invalidated
        r = self.service.add_source(item["id"], self._source("2026-09-29T11:00:00Z", 30), "analyst-1", "analyst")
        self.assertTrue(r["basis_changed"])
        self.assertIn("execute", r["blocked_actions"])
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "assessed")
        self.assertNotIn("approved_maneuver", item["payload"])
        # execute is now blocked because the approval was invalidated
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "execute", {"command_ref": "CMD-1"}, "operator-1", "operator", item["version"])
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_executed_maneuver_retains_original_basis(self):
        item = self._create_and_assess()
        self.service.add_source(item["id"], self._source("2026-09-29T10:00:00Z", 50), "analyst-1", "analyst")
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2.5, "maneuver_window": "w"}, "coordinator-1", "coordinator", item["version"])
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-1"}, "operator-1", "operator", item["version"])
        self.assertEqual(item["status"], "executing")
        approved_basis = item["payload"]["approved_maneuver"]["basis_version"]
        command_basis = item["payload"]["command_basis_version"]
        # a later observation changes the basis, but the issued execution is not unwound
        r = self.service.add_source(item["id"], self._source("2026-09-29T11:00:00Z", 30), "analyst-1", "analyst")
        self.assertTrue(r["basis_changed"])
        self.assertEqual(r["blocked_actions"], [])
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "executing")
        self.assertIn("approved_maneuver", item["payload"])
        self.assertEqual(item["payload"]["command_ref"], "CMD-1")
        self.assertEqual(item["payload"]["approved_maneuver"]["basis_version"], approved_basis)
        self.assertEqual(item["payload"]["command_basis_version"], command_basis)

    def test_notification_retry_only_resends_unfinished_steps(self):
        sender = RecordingSender(fail_step="notify_operator:Org-B")
        service = Service(self.repo, sender=sender)
        item = service.create_item(self.payload, "analyst-1", "analyst")
        item = service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        item = service.act(item["id"], "approve", {"fuel_cost_m_s": 2.5, "maneuver_window": "w"}, "coordinator-1", "coordinator", item["version"])
        notifications = service.list_notifications(item["id"])
        by_name = {n["step_name"]: n for n in notifications}
        self.assertEqual(by_name["notify_operator:Org-A"]["status"], "sent")
        self.assertEqual(by_name["notify_operator:Org-B"]["status"], "failed")
        # retry with a working sender: only the failed step is re-attempted
        working = RecordingSender()
        service.sender = working
        result = service.retry_notifications(item["id"])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(working.calls, ["notify_operator:Org-B"])
        notifications = service.list_notifications(item["id"])
        self.assertTrue(all(n["status"] == "sent" for n in notifications))

    def test_notification_progress_continues_after_restart(self):
        sender = RecordingSender(fail_step="notify_operator:Org-B")
        service1 = Service(self.repo, sender=sender)
        item = service1.create_item(self.payload, "analyst-1", "analyst")
        item = service1.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        item = service1.act(item["id"], "approve", {"fuel_cost_m_s": 2.5, "maneuver_window": "w"}, "coordinator-1", "coordinator", item["version"])
        # simulate a restart: a new service instance over the same database
        service2 = Service(self.repo, sender=RecordingSender())
        result = service2.retry_notifications(item["id"])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)
        notifications = service2.list_notifications(item["id"])
        self.assertTrue(all(n["status"] == "sent" for n in notifications))
        # the completed step was not re-sent after restart
        self.assertEqual(service2.sender.calls, ["notify_operator:Org-B"])

    def test_execute_notification_steps(self):
        sender = RecordingSender(fail_step="send_command")
        service = Service(self.repo, sender=sender)
        item = service.create_item(self.payload, "analyst-1", "analyst")
        item = service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        item = service.act(item["id"], "approve", {"fuel_cost_m_s": 2.5, "maneuver_window": "w"}, "coordinator-1", "coordinator", item["version"])
        item = service.act(item["id"], "execute", {"command_ref": "CMD-1"}, "operator-1", "operator", item["version"])
        notifications = service.list_notifications(item["id"])
        by_name = {n["step_name"]: n for n in notifications}
        self.assertEqual(by_name["send_command"]["status"], "failed")
        self.assertEqual(by_name["notify_execution"]["status"], "sent")
        # retry sends only the failed step
        service.sender = RecordingSender()
        result = service.retry_notifications(item["id"])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
