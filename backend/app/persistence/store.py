"""Persistencia mínima de overrides del humano (SQLite).

El humano confirma cada veredicto ("sí, sin factura" / "está en otra cuenta" /
"es de 2025"). Esas decisiones se guardan: son la materia prima de la v2
(emparejamiento fino). El esquema se identifica por (huella_libro, cuenta) para
que un mismo fichero recupere sus overrides de forma determinista.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS overrides (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    huella_libro  TEXT NOT NULL,
    codigo_cuenta TEXT NOT NULL,
    veredicto     TEXT NOT NULL,   -- SIN_FACTURA | EN_OTRA_CUENTA | EJERCICIO_ANTERIOR | OTRO
    nota          TEXT,
    autor         TEXT,
    creado_en     TEXT NOT NULL,
    UNIQUE(huella_libro, codigo_cuenta)
);
CREATE TABLE IF NOT EXISTS visibilidad_informe (
    huella_libro  TEXT NOT NULL,
    codigo_cuenta TEXT NOT NULL,
    mostrar       INTEGER NOT NULL,   -- 1 = se incluye en el informe PDF; 0 = oculto
    actualizado   TEXT NOT NULL,
    UNIQUE(huella_libro, codigo_cuenta)
);
"""

VEREDICTOS_VALIDOS = {
    "SIN_FACTURA",        # el humano confirma que no hay factura
    "EN_OTRA_CUENTA",     # la factura está en otra cuenta/epígrafe
    "EJERCICIO_ANTERIOR", # la factura es de un ejercicio no incluido
    "OTRO",
}


@dataclass(frozen=True)
class Override:
    huella_libro: str
    codigo_cuenta: str
    veredicto: str
    nota: str | None
    autor: str | None
    creado_en: str


class OverrideStore:
    def __init__(self, ruta_db: str | Path = "overrides.db") -> None:
        self.ruta_db = str(ruta_db)
        with self._conn() as c:
            c.executescript(_DDL)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ruta_db)
        conn.row_factory = sqlite3.Row
        return conn

    def guardar(self, huella_libro: str, codigo_cuenta: str, veredicto: str,
                nota: str | None = None, autor: str | None = None) -> Override:
        if veredicto not in VEREDICTOS_VALIDOS:
            raise ValueError(
                f"Veredicto no válido: {veredicto}. Use {sorted(VEREDICTOS_VALIDOS)}"
            )
        creado = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """INSERT INTO overrides
                   (huella_libro, codigo_cuenta, veredicto, nota, autor, creado_en)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(huella_libro, codigo_cuenta) DO UPDATE SET
                     veredicto=excluded.veredicto, nota=excluded.nota,
                     autor=excluded.autor, creado_en=excluded.creado_en""",
                (huella_libro, codigo_cuenta, veredicto, nota, autor, creado),
            )
        return Override(huella_libro, codigo_cuenta, veredicto, nota, autor, creado)

    def listar(self, huella_libro: str) -> dict[str, Override]:
        with self._conn() as c:
            filas = c.execute(
                "SELECT * FROM overrides WHERE huella_libro=?", (huella_libro,)
            ).fetchall()
        return {
            f["codigo_cuenta"]: Override(
                f["huella_libro"], f["codigo_cuenta"], f["veredicto"],
                f["nota"], f["autor"], f["creado_en"],
            )
            for f in filas
        }

    # --- Visibilidad en el informe PDF -------------------------------------
    def set_visibilidad(self, huella_libro: str, codigo_cuenta: str,
                        mostrar: bool) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO visibilidad_informe
                   (huella_libro, codigo_cuenta, mostrar, actualizado)
                   VALUES (?,?,?,?)
                   ON CONFLICT(huella_libro, codigo_cuenta) DO UPDATE SET
                     mostrar=excluded.mostrar, actualizado=excluded.actualizado""",
                (huella_libro, codigo_cuenta, 1 if mostrar else 0,
                 datetime.now(timezone.utc).isoformat()),
            )

    def ocultos(self, huella_libro: str) -> set[str]:
        """Cuentas marcadas para NO mostrar en el informe (mostrar=0)."""
        with self._conn() as c:
            filas = c.execute(
                "SELECT codigo_cuenta FROM visibilidad_informe "
                "WHERE huella_libro=? AND mostrar=0", (huella_libro,)
            ).fetchall()
        return {f["codigo_cuenta"] for f in filas}

    def visibilidad(self, huella_libro: str) -> dict[str, bool]:
        """Mapa explícito {codigo: mostrar} de las decisiones guardadas."""
        with self._conn() as c:
            filas = c.execute(
                "SELECT codigo_cuenta, mostrar FROM visibilidad_informe "
                "WHERE huella_libro=?", (huella_libro,)
            ).fetchall()
        return {f["codigo_cuenta"]: bool(f["mostrar"]) for f in filas}
