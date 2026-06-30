"""Persistencia mínima de la conciliación: overrides de revisión + audit_log."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_RUTA = os.getenv("GESTIONA_CONCILIACION_DB") or str(
    Path(__file__).parent.parent.parent.parent / "conciliacion.db")

_DDL = """
CREATE TABLE IF NOT EXISTS overrides (
    project_id  TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    code        TEXT NOT NULL,
    UNIQUE(project_id, entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT, actor TEXT, action TEXT,
    entity_type TEXT, entity_id TEXT,
    before TEXT, after TEXT, ts TEXT NOT NULL
);
"""


class ConciliacionStore:
    def __init__(self, ruta: str | Path = _RUTA) -> None:
        self.ruta = str(ruta)
        with self._c() as c:
            c.executescript(_DDL)

    def _c(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ruta)
        conn.row_factory = sqlite3.Row
        return conn

    def set_override(self, pid: str, tipo: str, entity_id: str, code: str) -> None:
        with self._c() as c:
            c.execute(
                """INSERT INTO overrides (project_id, entity_type, entity_id, code)
                   VALUES (?,?,?,?)
                   ON CONFLICT(project_id, entity_type, entity_id)
                   DO UPDATE SET code=excluded.code""",
                (pid, tipo, entity_id, code))

    def overrides(self, pid: str) -> dict[tuple[str, str], str]:
        with self._c() as c:
            filas = c.execute(
                "SELECT entity_type, entity_id, code FROM overrides WHERE project_id=?",
                (pid,)).fetchall()
        return {(f["entity_type"], f["entity_id"]): f["code"] for f in filas}

    def audit(self, pid: str, actor: str, action: str, entity_type: str,
              entity_id: str, before=None, after=None) -> None:
        with self._c() as c:
            c.execute(
                """INSERT INTO audit_log
                   (project_id, actor, action, entity_type, entity_id, before, after, ts)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (pid, actor, action, entity_type, entity_id,
                 json.dumps(before, ensure_ascii=False) if before is not None else None,
                 json.dumps(after, ensure_ascii=False) if after is not None else None,
                 datetime.now(timezone.utc).isoformat()))

    def historial(self, pid: str, limite: int = 200) -> list[dict]:
        with self._c() as c:
            filas = c.execute(
                "SELECT * FROM audit_log WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (pid, limite)).fetchall()
        return [dict(f) for f in filas]


store = ConciliacionStore()
