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
                    segment_key TEXT,
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
                CREATE TABLE IF NOT EXISTS valve_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    receipt_id TEXT NOT NULL,
                    valve_id TEXT NOT NULL,
                    result TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, receipt_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    version INTEGER,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # 兼容旧库：逐列补齐，避免重复建单/审计版本等功能在旧数据上缺失
            self._ensure_column(conn, "items", "segment_key", "TEXT")
            self._ensure_column(conn, "audit_events", "version", "INTEGER")
        finally:
            conn.close()

    def _ensure_column(self, conn, table, column, decl):
        columns = [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
        if column not in columns:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))

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

    def append_audit(self, conn, item_id, event_type, actor, role, payload, version=None):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        if version is not None:
            event["version"] = version
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,version,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, version, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, segment_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,segment_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        segment_key,
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
            # 首条报告本身就是第一个来源，并入合并报告清单
            source_type = payload.get("source_type", "alarm")
            external_id = payload.get("external_id", actor)
            observed_at = payload.get("reported_at", now_iso())
            conn.execute(
                "INSERT OR IGNORE INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                (
                    item_id,
                    source_type,
                    external_id,
                    canonical_json({"reporter": payload.get("reporter", actor)}),
                    observed_at,
                    now_iso(),
                ),
            )
            source_id = conn.execute(
                "SELECT id FROM sources WHERE item_id=? AND source_type=? AND external_id=?",
                (item_id, source_type, external_id),
            ).fetchone()["id"]
            payload["merged_reports"] = [{
                "source_id": source_id,
                "source_type": source_type,
                "external_id": external_id,
                "observed_at": observed_at,
                "readings": {
                    "pressure_drop_kpa": payload.get("pressure_drop_kpa", 0),
                    "sensor_value_ppm": payload.get("sensor_value_ppm", 0),
                    "odor_reports": payload.get("odor_reports", 0),
                },
            }]
            conn.execute(
                "UPDATE items SET payload=? WHERE id=?",
                (canonical_json(payload), item_id),
            )
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key, "segment_key": segment_key}, 1)
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

    def find_by_stable_key(self, entity_type, stable_key):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM items WHERE entity_type=? AND stable_key=? LIMIT 1",
                (entity_type, stable_key),
            ).fetchone()
            return self._row_to_item(row) if row else None
        finally:
            conn.close()

    def find_active_by_segment(self, entity_type, segment_key, terminal_statuses):
        conn = self.connect()
        try:
            placeholders = ",".join("?" for _ in terminal_statuses)
            row = conn.execute(
                "SELECT * FROM items WHERE entity_type=? AND segment_key=? AND status NOT IN (%s) ORDER BY id DESC LIMIT 1" % placeholders,
                (entity_type, segment_key, *terminal_statuses),
            ).fetchone()
            return self._row_to_item(row) if row else None
        finally:
            conn.close()

    def source_exists(self, item_id, source_type, external_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT id FROM sources WHERE item_id=? AND source_type=? AND external_id=? LIMIT 1",
                (item_id, source_type, external_id),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def merge_alarm(self, entity_type, item_id, segment_key, source, readings, actor, role):
        """同一管段的在途报警合并为一个事件：来源并存，读数取最不利值，版本递增并留审计。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            payload = json.loads(row["payload"])
            # 来源记录幂等：同一 (item, source_type, external_id) 只保留一份
            conn.execute(
                "INSERT OR IGNORE INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                (
                    item_id,
                    source["source_type"],
                    source["external_id"],
                    canonical_json(source.get("payload", {})),
                    source["observed_at"],
                    now_iso(),
                ),
            )
            source_row = conn.execute(
                "SELECT id FROM sources WHERE item_id=? AND source_type=? AND external_id=?",
                (item_id, source["source_type"], source["external_id"]),
            ).fetchone()
            merged = dict(payload)
            merged["pressure_drop_kpa"] = max(float(payload.get("pressure_drop_kpa", 0)), readings["pressure_drop_kpa"])
            merged["sensor_value_ppm"] = max(float(payload.get("sensor_value_ppm", 0)), readings["sensor_value_ppm"])
            merged["odor_reports"] = max(int(payload.get("odor_reports", 0)), readings["odor_reports"])
            merged_reports = list(payload.get("merged_reports", []))
            merged_reports.append({
                "source_id": source_row["id"],
                "source_type": source["source_type"],
                "external_id": source["external_id"],
                "observed_at": source["observed_at"],
                "readings": readings,
            })
            merged["merged_reports"] = merged_reports
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET payload=?,version=?,updated_at=? WHERE id=?",
                (canonical_json(merged), version, now_iso(), item_id),
            )
            self.append_audit(
                conn,
                item_id,
                "alarm_merged",
                actor,
                role,
                {
                    "source_id": source_row["id"],
                    "source_type": source["source_type"],
                    "external_id": source["external_id"],
                    "readings": readings,
                },
                version,
            )
            conn.execute("COMMIT")
            result = self.get_item(item_id)
            result["merged"] = True
            return result
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
            item = conn.execute("SELECT id, version FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            version = int(item["version"]) + 1
            conn.execute("UPDATE items SET version=?,updated_at=? WHERE id=?", (version, now_iso(), item_id))
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
                version,
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
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
            if action == "valve_receipt":
                # 回执落库：receipt_id 唯一，重复回执直接忽略，不重复执行
                for receipt in new_payload.get("valve_receipts", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO valve_receipts(item_id,receipt_id,valve_id,result,payload,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            item_id,
                            receipt["receipt_id"],
                            receipt["valve_id"],
                            receipt["result"],
                            canonical_json({k: v for k, v in receipt.items() if k != "actor"}),
                            now_iso(),
                        ),
                    )
            self.append_audit(conn, item_id, action, actor, role, event_payload, version)
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

    def list_valve_receipts(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM valve_receipts WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def verify_chain(self, item_id):
        """重算审计哈希链：任何篡改、断链或版本缺失都能被发现。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
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
                if row["version"] is not None:
                    event["version"] = row["version"]
                expected = audit_hash(previous, event)
                if expected != row["event_hash"] or row["previous_hash"] != previous:
                    return {"valid": False, "broken_at": row["id"], "events": len(rows)}
                previous = row["event_hash"]
            return {"valid": True, "events": len(rows)}
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
