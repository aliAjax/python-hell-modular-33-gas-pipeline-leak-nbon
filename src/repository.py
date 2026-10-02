import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


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
                    merge_key TEXT,
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
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "merge_key" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN merge_key TEXT")
        rows = conn.execute("SELECT id, payload FROM items WHERE merge_key IS NULL").fetchall()
        for row in rows:
            data = json.loads(row["payload"])
            merge_key = "%s|%s" % (data.get("pipeline_id", ""), data.get("segment_id", ""))
            conn.execute("UPDATE items SET merge_key=? WHERE id=?", (merge_key, row["id"]))

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
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def _insert_source(self, conn, item_id, source):
        try:
            conn.execute(
                "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                (
                    item_id,
                    source["source_type"],
                    source["external_id"],
                    canonical_json(source["payload"]),
                    source["observed_at"],
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def _merge_source_values(self, item_payload, source):
        comparison = []
        data = source.get("payload") or {}
        for field in ("pressure_drop_kpa", "sensor_value_ppm", "odor_reports"):
            new_value = data.get(field)
            if new_value is None:
                continue
            old_value = item_payload.get(field, 0) or 0
            if float(new_value) > float(old_value):
                comparison.append({
                    "field": field,
                    "old": old_value,
                    "new": new_value,
                    "source_type": source["source_type"],
                    "external_id": source["external_id"],
                })
                item_payload[field] = new_value
        return comparison

    def create_or_merge(self, entity_type, merge_key, initial_status, payload, source, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM items WHERE entity_type=? AND merge_key=? AND status NOT IN ('restored','cancelled') ORDER BY id DESC LIMIT 1",
                (entity_type, merge_key),
            ).fetchone()
            if row is not None:
                item_id = row["id"]
                source_id = self._insert_source(conn, item_id, source)
                if source_id is not None:
                    item_payload = json.loads(row["payload"])
                    comparison = self._merge_source_values(item_payload, source)
                    if comparison:
                        item_payload["source_comparison"] = list(item_payload.get("source_comparison") or []) + comparison
                        conn.execute(
                            "UPDATE items SET payload=?,version=?,updated_at=? WHERE id=?",
                            (canonical_json(item_payload), int(row["version"]) + 1, now_iso(), item_id),
                        )
                    self.append_audit(
                        conn,
                        item_id,
                        "source_recorded",
                        actor,
                        role,
                        {
                            "source_id": source_id,
                            "source_type": source["source_type"],
                            "external_id": source["external_id"],
                            "merged_into": item_id,
                            "comparison": comparison,
                        },
                    )
                conn.execute("COMMIT")
                return self.get_item(item_id), False
            seq = conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE entity_type=? AND merge_key=?",
                (entity_type, merge_key),
            ).fetchone()["n"] + 1
            stable_key = "%s#%d" % (merge_key, seq)
            conn.execute(
                "INSERT INTO items(entity_type,stable_key,merge_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    entity_type,
                    stable_key,
                    merge_key,
                    initial_status,
                    1,
                    canonical_json(payload),
                    actor,
                    role,
                    now_iso(),
                    now_iso(),
                ),
            )
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            source_id = self._insert_source(conn, item_id, source)
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key, "merge_key": merge_key})
            if source_id is not None:
                self.append_audit(
                    conn,
                    item_id,
                    "source_recorded",
                    actor,
                    role,
                    {"source_id": source_id, "source_type": source["source_type"], "external_id": source["external_id"]},
                )
            conn.execute("COMMIT")
            return self.get_item(item_id), True
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

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            source_id = self._insert_source(
                conn,
                item_id,
                {
                    "source_type": source_type,
                    "external_id": external_id,
                    "payload": payload,
                    "observed_at": observed_at,
                },
            )
            if source_id is None:
                conn.execute("COMMIT")
                existing = self._find_source(item_id, source_type, external_id)
                existing["duplicate"] = True
                return existing, False
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {
                "id": source_id,
                "item_id": item_id,
                "source_type": source_type,
                "external_id": external_id,
                "payload": payload,
                "observed_at": observed_at,
            }, True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _find_source(self, item_id, source_type, external_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sources WHERE item_id=? AND source_type=? AND external_id=?",
                (item_id, source_type, external_id),
            ).fetchone()
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            return value
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
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
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
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

    def verify_audit_chain(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            previous = "GENESIS"
            for row in rows:
                event = {
                    "item_id": row["item_id"],
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "role": row["role"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
                if row["previous_hash"] != previous:
                    return False
                if audit_hash(previous, event) != row["event_hash"]:
                    return False
                previous = row["event_hash"]
            return True
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
