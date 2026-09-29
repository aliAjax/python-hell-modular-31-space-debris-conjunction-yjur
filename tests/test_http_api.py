import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.http_api import build_handler


CREATE_PAYLOAD = {
    "primary_object_id": "SAT-HTTP",
    "secondary_object_id": "DEB-HTTP",
    "tca": "2026-10-01T12:00:00+00:00",
    "miss_distance_m": 80,
    "covariance_m": 100,
    "fuel_budget_m_s": 4,
    "track_age_hours": 1,
    "operating_organizations": ["Org-A", "Org-B"],
}


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        repo = Repository(self.tmp.name)
        repo.initialize()
        self.service = Service(repo)
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.service, static_dir))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.unlink(self.tmp.name)

    def _request(self, method, path, body=None, user="a", role="analyst"):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-User-Id", user)
        req.add_header("X-Role", role)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_source_pending_confirm_and_dispatch_flow(self):
        status, item = self._request("POST", "/api/items", CREATE_PAYLOAD)
        self.assertEqual(status, 201)
        item_id = item["id"]
        status, item = self._request("POST", "/api/items/%d/actions" % item_id,
                                     {"action": "assess", "hours_to_tca": 18, "expected_version": item["version"]})
        self.assertEqual(status, 200)

        # 两个站同一观测时刻提交
        status, first = self._request("POST", "/api/items/%d/sources" % item_id, {
            "source_type": "radar", "external_id": "ST-A",
            "observed_at": "2026-10-01T06:00:00+00:00",
            "miss_distance_m": 80, "covariance_m": 100, "expected_version": item["version"],
        })
        self.assertEqual(status, 201)
        self.assertEqual(first["source_result"]["state"], "current")
        status, second = self._request("POST", "/api/items/%d/sources" % item_id, {
            "source_type": "radar", "external_id": "ST-B",
            "observed_at": "2026-10-01T06:00:00+00:00",
            "miss_distance_m": 200, "covariance_m": 100, "expected_version": first["version"],
        })
        self.assertEqual(second["source_result"]["state"], "pending")
        pending_id = second["source_result"]["source_id"]

        # 批准（基于当前依据 ST-A）
        status, approved = self._request("POST", "/api/items/%d/actions" % item_id, {
            "action": "approve", "fuel_cost_m_s": 2.0, "maneuver_window": "w1",
            "expected_version": second["version"],
        }, user="c", role="coordinator")
        self.assertEqual(status, 200)
        plan_id = approved["notification_plan"]["plan_id"]

        # 协调员确认待确认来源 ST-B：批准失效、受阻动作列出
        status, confirmed = self._request("POST", "/api/items/%d/actions" % item_id, {
            "action": "confirm_source", "source_id": pending_id,
            "expected_version": approved["version"],
        }, user="c", role="coordinator")
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "assessed")
        self.assertEqual(len(confirmed["blocked_actions"]), 1)

        # 旧通知计划已作废
        status, dispatch = self._request("POST", "/api/plans/%d/dispatch" % plan_id,
                                         {}, user="c", role="coordinator")
        self.assertEqual(status, 200)
        self.assertEqual(dispatch["status"], "superseded")


if __name__ == "__main__":
    unittest.main()
