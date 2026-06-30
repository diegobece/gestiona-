"""Almacén persistente de usuarios registrados (SQLite).

Lo usan tanto el registro de la web como `crear_usuario.py`. Guarda solo el hash
de la contraseña, nunca en claro. Es la fuente principal de cuentas; `config.USUARIOS`
(users.json / variables de entorno / admin de dev) actúa como respaldo.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_RUTA = os.getenv("GESTIONA_USERS_DB") or str(
    Path(__file__).parent.parent.parent / "usuarios.db")

_DDL = """
CREATE TABLE IF NOT EXISTS usuarios (
    usuario       TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    creado_en     TEXT NOT NULL
);
"""

_RE_USUARIO = re.compile(r"^[A-Za-z0-9._@-]{3,32}$")


def usuario_valido(usuario: str) -> bool:
    return bool(_RE_USUARIO.match(usuario or ""))


class UsuarioStore:
    def __init__(self, ruta: str | Path = _RUTA) -> None:
        self.ruta = str(ruta)
        with self._conn() as c:
            c.executescript(_DDL)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ruta)
        conn.row_factory = sqlite3.Row
        return conn

    def obtener(self, usuario: str) -> str | None:
        with self._conn() as c:
            fila = c.execute(
                "SELECT password_hash FROM usuarios WHERE usuario=?", (usuario,)
            ).fetchone()
        return fila["password_hash"] if fila else None

    def existe(self, usuario: str) -> bool:
        return self.obtener(usuario) is not None

    def crear_o_actualizar(self, usuario: str, password_hash: str) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO usuarios (usuario, password_hash, creado_en)
                   VALUES (?,?,?)
                   ON CONFLICT(usuario) DO UPDATE SET password_hash=excluded.password_hash""",
                (usuario, password_hash, datetime.now(timezone.utc).isoformat()),
            )

    def listar(self) -> list[str]:
        with self._conn() as c:
            return [r["usuario"] for r in c.execute(
                "SELECT usuario FROM usuarios ORDER BY usuario").fetchall()]


# Instancia compartida.
store = UsuarioStore()
