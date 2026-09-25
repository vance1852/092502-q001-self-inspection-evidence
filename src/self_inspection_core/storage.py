"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inspection_checklists (
    checklist_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    local_date TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    source_fingerprint TEXT NOT NULL,
    generated_by TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    UNIQUE(site_id, local_date, version)
);
CREATE TABLE IF NOT EXISTS inspection_items (
    item_id TEXT PRIMARY KEY,
    checklist_id TEXT NOT NULL REFERENCES inspection_checklists(checklist_id),
    item_key TEXT NOT NULL,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    requirement TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE(checklist_id, item_key)
);
CREATE TABLE IF NOT EXISTS inspection_evidence (
    evidence_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    local_date TEXT NOT NULL,
    item_key TEXT NOT NULL,
    checklist_id TEXT NOT NULL REFERENCES inspection_checklists(checklist_id),
    checklist_version INTEGER NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    evidence_hash TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    clock_skew_seconds REAL NOT NULL,
    late INTEGER NOT NULL CHECK(late IN (0, 1)),
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    received_at TEXT NOT NULL,
    UNIQUE(site_id, local_date, item_key, version)
);
CREATE INDEX IF NOT EXISTS idx_inspection_evidence_item
    ON inspection_evidence(site_id, local_date, item_key);
CREATE TABLE IF NOT EXISTS inspection_corrections (
    correction_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    local_date TEXT NOT NULL,
    item_key TEXT NOT NULL,
    checklist_id TEXT NOT NULL REFERENCES inspection_checklists(checklist_id),
    checklist_version INTEGER NOT NULL,
    content TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspection_corrections_item
    ON inspection_corrections(site_id, local_date, item_key);
CREATE TABLE IF NOT EXISTS inspection_decisions (
    decision_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    local_date TEXT NOT NULL,
    item_key TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('dispute', 'accept', 'request_supplement')),
    evidence_id TEXT REFERENCES inspection_evidence(evidence_id),
    note TEXT NOT NULL,
    checklist_id TEXT NOT NULL REFERENCES inspection_checklists(checklist_id),
    checklist_version INTEGER NOT NULL,
    decided_by TEXT NOT NULL REFERENCES actors(actor_id),
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspection_decisions_item
    ON inspection_decisions(site_id, local_date, item_key);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；并发调用按先后串行执行。"""

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
