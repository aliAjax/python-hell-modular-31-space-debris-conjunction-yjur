import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'history',
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS notification_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    basis_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS notification_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    step_index INTEGER NOT NULL,
                    recipient TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_run_token TEXT,
                    sent_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(plan_id, step_index),
                    FOREIGN KEY(plan_id) REFERENCES notification_plans(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate(conn)
        finally:
            conn.close()

    def _migrate(self, conn):
        # 兼容已存在的库：补齐后加的列。
        source_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "state" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN state TEXT NOT NULL DEFAULT 'history'")
        step_columns = {row["name"] for row in conn.execute("PRAGMA table_info(notification_steps)").fetchall()}
        if step_columns and "last_run_token" not in step_columns:
            conn.execute("ALTER TABLE notification_steps ADD COLUMN last_run_token TEXT")

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        cur = conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )
        return cur.lastrowid

    def _insert_action(self, conn, item_id, action, actor, role, event_payload):
        conn.execute(
            "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
            (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def list_sources(self, conn, item_id):
        rows = conn.execute(
            "SELECT * FROM sources WHERE item_id=? ORDER BY id ASC", (item_id,)
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    def _supersede_plans(self, conn, item_id):
        """依据变化使批准失效：该事件尚未发完的通知计划一并作废，避免旧指令外发。"""
        superseded = []
        rows = conn.execute(
            "SELECT id FROM notification_plans WHERE item_id=? AND status='active'",
            (item_id,),
        ).fetchall()
        for row in rows:
            plan_id = row["id"]
            conn.execute(
                "UPDATE notification_plans SET status='superseded', updated_at=? WHERE id=? AND status='active'",
                (now_iso(), plan_id),
            )
            conn.execute(
                "UPDATE notification_steps SET state='superseded', updated_at=? "
                "WHERE plan_id=? AND state IN ('pending','failed','sending')",
                (now_iso(), plan_id),
            )
            superseded.append(plan_id)
        return superseded

    def _apply_basis_events(self, conn, item_id, decision, actor, role):
        superseded_plans = []
        if decision.get("blocked"):
            superseded_plans = self._supersede_plans(conn, item_id)
        for extra in decision.get("extra_events", []):
            payload = dict(extra.get("payload", {}))
            if extra.get("event_type") == "approval_invalidated" and superseded_plans:
                payload["superseded_plans"] = list(superseded_plans)
            self.append_audit(conn, item_id, extra["event_type"], actor, role, payload)
        return superseded_plans

    def ingest_source(self, item_id, normalized, actor, role, decide, expected_version=None):
        """在单个事务中落来源、计算依据决策、推进同一条版本链。

        decide(current_payload, existing_sources, new_source, new_source_id) -> 决策字典
        （见 rules.source_decision）。
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            item = self._row_to_item(row)
            source_type = normalized["source_type"]
            external_id = normalized["external_id"]
            observed_at = normalized["observed_at"]
            source_payload = normalized["payload"]
            try:
                cur = conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,state,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(source_payload), observed_at, "history", now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = cur.lastrowid

            decision = decide(item["payload"], self.list_sources(conn, item_id), source_payload, source_id)
            self._persist_source_decision(conn, item_id, item, decision, source_id, actor, role)
            source_row = self.get_source(conn, source_id)
            conn.execute("COMMIT")
            return source_row, decision
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def confirm_source(self, item_id, source_id, actor, role, decide, expected_version=None):
        """协调员确认待确认来源，依据版本链向前推进。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            item = self._row_to_item(row)
            sources = self.list_sources(conn, item_id)
            decision = decide(item["payload"], sources, source_id)
            self._persist_source_decision(conn, item_id, item, decision, source_id, actor, role)
            source_row = self.get_source(conn, source_id)
            conn.execute("COMMIT")
            return source_row, decision
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _persist_source_decision(self, conn, item_id, item, decision, source_id, actor, role):
        version = int(item["version"])
        new_state = decision["new_state"]
        conn.execute(
            "UPDATE sources SET state=? WHERE id=?", (new_state, source_id)
        )

        next_payload = decision.get("next_payload")
        superseded_plans = []
        if decision.get("becomes_current"):
            version += 1
            old_current = decision.get("old_current_source_id")
            if old_current is not None:
                conn.execute(
                    "UPDATE sources SET state='history' WHERE id=? AND state='current'",
                    (old_current,),
                )
            # 同时刻遗留的待确认来源：新当前依据确认后只留历史。
            conn.execute(
                "UPDATE sources SET state='history' WHERE item_id=? AND state='pending' AND id<>?",
                (item_id, source_id),
            )
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (decision.get("new_status", item["status"]), version, canonical_json(next_payload), now_iso(), item_id),
            )
            superseded_plans = self._apply_basis_events(conn, item_id, decision, actor, role)

        self._insert_action(conn, item_id, decision.get("event_type", "source_recorded"), actor, role, decision["event_payload"])
        self.append_audit(
            conn,
            item_id,
            decision.get("event_type", "source_recorded"),
            actor,
            role,
            decision["event_payload"],
        )
        if superseded_plans:
            decision["superseded_plans"] = superseded_plans

    def get_source(self, conn, source_id):
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise NotFoundError("source_not_found", "来源记录不存在")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def list_sources_public(self, item_id):
        conn = self.connect()
        try:
            return self.list_sources(conn, item_id)
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload,
                     expected_version=None, extra_events=None, side_effect=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            self._insert_action(conn, item_id, action, actor, role, event_payload)
            self.append_audit(conn, item_id, action, actor, role, event_payload)

            # 批准失效类附加事件（如 report_revision 使批准失效），并作废旧通知计划。
            superseded_plans = []
            for extra in extra_events or []:
                payload = dict(extra.get("payload", {}))
                if extra.get("event_type") == "approval_invalidated":
                    superseded_plans = self._supersede_plans(conn, item_id)
                    if superseded_plans:
                        payload["superseded_plans"] = list(superseded_plans)
                self.append_audit(conn, item_id, extra["event_type"], actor, role, payload)

            side_result = None
            if side_effect is not None:
                # 与状态变更同一事务：批准与通知计划要么同时生效要么同时不生效。
                side_result = side_effect(conn, item_id, new_payload, new_status, version, actor, role)
            conn.execute("COMMIT")
            return self.get_item(item_id), side_result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # ---------------- 通知计划与分步派发 ----------------

    def create_notification_plan(self, conn, item_id, payload, approved_maneuver):
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO notification_plans(item_id,basis_version,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (item_id, approved_maneuver.get("basis_version"), "active", now, now),
        )
        plan_id = cur.lastrowid
        from .notifications import build_steps

        steps = build_steps(item_id, plan_id, payload, approved_maneuver)
        for step in steps:
            conn.execute(
                "INSERT INTO notification_steps(plan_id,item_id,step_index,recipient,channel,subject,body,"
                "idempotency_key,state,attempts,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, item_id, step["step_index"], step["recipient"], step["channel"],
                    step["subject"], step["body"], step["idempotency_key"], "pending", 0, now,
                ),
            )
        status = "completed" if not steps else "active"
        conn.execute(
            "UPDATE notification_plans SET status=?,updated_at=? WHERE id=?", (status, now, plan_id)
        )
        self.append_audit(conn, item_id, "notification_plan_created", None, "system",
                          {"plan_id": plan_id, "basis_version": approved_maneuver.get("basis_version"),
                           "steps": len(steps), "status": status})
        return {"plan_id": plan_id, "steps": len(steps), "status": status}

    def get_plan(self, plan_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM notification_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise NotFoundError("plan_not_found", "通知计划不存在")
            return dict(row)
        finally:
            conn.close()

    def list_plans(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM notification_plans WHERE item_id=? ORDER BY id ASC", (item_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _recompute_plan_status(self, conn, plan_id):
        counts = {state: 0 for state in ("pending", "sending", "failed", "sent", "superseded")}
        rows = conn.execute(
            "SELECT state, COUNT(*) AS total FROM notification_steps WHERE plan_id=? GROUP BY state",
            (plan_id,),
        ).fetchall()
        for row in rows:
            counts[row["state"]] = row["total"]
        unfinished = counts["pending"] + counts["sending"] + counts["failed"]
        sent = counts["sent"]
        superseded = counts["superseded"]
        if unfinished == 0:
            if superseded == 0:
                status = "completed"
            elif sent == 0:
                status = "superseded"
            else:
                status = "completed_partial"
        elif counts["failed"]:
            status = "failed"
        else:
            status = "active"
        conn.execute(
            "UPDATE notification_plans SET status=?,updated_at=? WHERE id=?", (status, now_iso(), plan_id)
        )
        return status

    def claim_next_step(self, plan_id, run_token=None):
        """在事务中领取下一条未完成步骤。已完成的步骤永不重复下发。

        run_token 标识一次派发轮次：同一轮中刚失败的步骤不会被立刻重复领取，
        它留到下一轮（重试）处理；不带 run_token 时领取任意未完成步骤。
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = conn.execute(
                "SELECT * FROM notification_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFoundError("plan_not_found", "通知计划不存在")
            if plan["status"] == "superseded":
                conn.execute("COMMIT")
                return None
            from .notifications import RETRYABLE_STATES

            states = list(RETRYABLE_STATES)
            placeholders = ",".join("?" for _ in states)
            query = (
                "SELECT * FROM notification_steps WHERE plan_id=? AND state IN (%s)" % placeholders
            )
            params = [plan_id, *states]
            if run_token is not None:
                # 同一轮中刚失败的步骤留到下一轮重试
                query += " AND (last_run_token IS NULL OR last_run_token<>?)"
                params.append(run_token)
            query += " ORDER BY step_index ASC LIMIT 1"
            step = conn.execute(query, params).fetchone()
            if step is None:
                status = self._recompute_plan_status(conn, plan_id)
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE notification_steps SET state='sending', attempts=attempts+1, "
                "last_run_token=?, updated_at=? WHERE id=?",
                (run_token, now_iso(), step["id"]),
            )
            self.append_audit(conn, step["item_id"], "notification_attempt", None, "system",
                              {"plan_id": plan_id, "step_index": step["step_index"],
                               "recipient": step["recipient"], "attempt": step["attempts"] + 1})
            conn.execute("COMMIT")
            result = dict(step)
            result["attempts"] = step["attempts"] + 1
            result["state"] = "sending"
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def mark_step_sent(self, step_id):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            step = conn.execute("SELECT * FROM notification_steps WHERE id=?", (step_id,)).fetchone()
            if step is None:
                raise NotFoundError("step_not_found", "通知步骤不存在")
            if step["state"] != "sent":
                now = now_iso()
                conn.execute(
                    "UPDATE notification_steps SET state='sent', last_error=NULL, sent_at=?, updated_at=? WHERE id=?",
                    (now, now, step_id),
                )
                self.append_audit(conn, step["item_id"], "notification_sent", None, "system",
                                  {"plan_id": step["plan_id"], "step_index": step["step_index"],
                                   "recipient": step["recipient"]})
            status = self._recompute_plan_status(conn, step["plan_id"])
            conn.execute("COMMIT")
            return status
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def mark_step_failed(self, step_id, error):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            step = conn.execute("SELECT * FROM notification_steps WHERE id=?", (step_id,)).fetchone()
            if step is None:
                raise NotFoundError("step_not_found", "通知步骤不存在")
            conn.execute(
                "UPDATE notification_steps SET state='failed', last_error=?, updated_at=? WHERE id=?",
                (str(error)[:500], now_iso(), step_id),
            )
            self.append_audit(conn, step["item_id"], "notification_failed", None, "system",
                              {"plan_id": step["plan_id"], "step_index": step["step_index"],
                               "recipient": step["recipient"], "error": str(error)[:500]})
            status = self._recompute_plan_status(conn, step["plan_id"])
            conn.execute("COMMIT")
            return status
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
