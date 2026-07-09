"""Chequeo de integridad de saldos sobre los ficheros reales disponibles.

Este es el "último chequeo" antes de dar por bueno un análisis: para CADA
cuenta, el saldo reconstruido por el motor (apertura + Σ Debe − Σ Haber) debe
coincidir, dentro de la tolerancia, con el saldo que el propio fichero trae en
su columna de saldo. Si cuadran todos, los datos del Excel se cargaron fielmente
(no se perdió, duplicó ni desplazó ningún apunte) y ningún saldo mostrado en la
plataforma es inventado.

Caso que motivó el test: INTEGRATED SOLUTIONS MADRID SL (4100024) mostraba en el
navegador un saldo espurio (3344,71 €) que resultó ser estado viejo cacheado; el
motor calcula −539,27 €, idéntico al saldo del fichero. Ver [[errores-libros-reales]].
"""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingest.excel_parser import parsear_excel

TOL = Decimal("0.01")

# Ficheros reales candidatos (gitignored; el test se salta los que no estén).
RAIZ = Path(__file__).resolve().parents[2]
DOWNLOADS = Path.home() / "Downloads"
FICHEROS_REALES = [
    RAIZ / "FICHAS MAYOR.xlsx",
    DOWNLOADS / "TemporalFichasMayor_20260611_112723.xlsx",
]

DISPONIBLES = [f for f in FICHEROS_REALES if f.exists()]


def _saldos_por_cuenta(libro):
    """Devuelve {codigo: (saldo_reconstruido, saldo_reportado, nombre)}."""
    cuentas: "OrderedDict[str, list]" = OrderedDict()
    for m in libro.movimientos:
        cuentas.setdefault(m.codigo_cuenta, []).append(m)

    salida = {}
    for cod, movs in cuentas.items():
        ap = libro.aperturas.get(cod)
        apertura = ap.saldo_apertura if ap else Decimal("0")
        recon = (apertura
                 + sum((m.debe for m in movs), Decimal("0"))
                 - sum((m.haber for m in movs), Decimal("0"))).quantize(Decimal("0.01"))
        reportado = next(
            (m.saldo_reportado for m in reversed(movs)
             if m.saldo_reportado is not None), None)
        salida[cod] = (recon, reportado, movs[0].nombre_cuenta)
    return salida


@pytest.mark.skipif(not DISPONIBLES, reason="No hay ficheros reales disponibles")
@pytest.mark.parametrize("ruta", DISPONIBLES, ids=lambda p: p.name)
def test_todos_los_saldos_cuadran_con_el_fichero(ruta):
    """Cada cuenta reconstruye exactamente el saldo que trae el Excel."""
    libro = parsear_excel(ruta)
    saldos = _saldos_por_cuenta(libro)
    assert saldos, f"{ruta.name}: no se leyó ninguna cuenta"

    descuadres = []
    for cod, (recon, reportado, nombre) in saldos.items():
        if reportado is None:
            continue  # cuenta sin columna de saldo en el origen: no se puede validar
        if abs(recon - reportado) > TOL:
            descuadres.append(f"{cod} {nombre}: recon={recon} vs fichero={reportado}")

    assert not descuadres, (
        f"{ruta.name}: {len(descuadres)} cuenta(s) con saldo descuadrado:\n"
        + "\n".join(descuadres))


@pytest.mark.skipif(
    not (RAIZ / "FICHAS MAYOR.xlsx").exists(),
    reason="FICHAS MAYOR.xlsx no disponible")
def test_integrated_solutions_saldo_es_fiel_al_fichero():
    """Regresión del caso real: INTEGRATED SOLUTIONS = −539,27 € (no 3344,71)."""
    libro = parsear_excel(RAIZ / "FICHAS MAYOR.xlsx")
    saldos = _saldos_por_cuenta(libro)
    recon, reportado, _ = saldos["4100024"]
    assert reportado == Decimal("-539.27")
    assert recon == Decimal("-539.27")
