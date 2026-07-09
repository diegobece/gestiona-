"""Integración contra los ficheros reales de muestra (si están disponibles).

Valida que el flujo completo ingesta->motor se comporta como esperamos sobre
los datos reales, y que Excel y PDF coinciden en el veredicto por cuenta.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decimal import Decimal

from app.domain.models import Clasificacion, Origen
from app.engine.detector import MotorDeteccion
from app.engine.facturas import MotorFacturasSinPago
from app.ingest.excel_parser import parsear_excel
from app.ingest.pdf_parser import parsear_pdf

DOWNLOADS = Path.home() / "Downloads"
EXCEL = DOWNLOADS / "TemporalFichasMayor_20260611_112723.xlsx"
PDF = DOWNLOADS / "Fichas de mayor configurable (vertical) 110626132546.pdf"
ARTRIP = DOWNLOADS / "ARTRIP 25-26.xlsx"
RAIZ = Path(__file__).resolve().parents[2]

# Ficheros reales disponibles para los tests de invariante (gitignored: se saltan).
REALES = [p for p in (EXCEL, ARTRIP, RAIZ / "FICHAS MAYOR.xlsx") if p.exists()]

motor = MotorDeteccion()
motor_fsp = MotorFacturasSinPago()


@pytest.mark.skipif(not EXCEL.exists(), reason="Excel de muestra no disponible")
def test_excel_real_clasifica_y_excluye_tecnica():
    inf = motor.analizar(parsear_excel(EXCEL))
    por_codigo = {r.codigo_cuenta: r for r in inf.resultados}

    # La cuenta técnica debe quedar EXCLUIDA (no falso positivo).
    assert por_codigo["4009000"].clasificacion == Clasificacion.EXCLUIDA

    # Todas las cuentas reales deben reconstruir saldo (0 NO_FIABLE en esta muestra).
    assert inf.resumen.n_no_fiables == 0

    # Hay alguna cuenta de alta confianza (proveedores con pago y sin factura).
    assert inf.resumen.n_sin_factura >= 1

    # 4000109 (AMAZON EU) NO debe afirmarse: AMAZON tiene facturas en 4100091.
    amazon = por_codigo["4000109"]
    assert amazon.clasificacion == Clasificacion.REVISAR
    assert amazon.subcategoria == "FACTURA_EN_OTRA_CUENTA"
    assert "4100091" in amazon.subcategoria_motivo

    # Ninguna afirmación sin pagos. Una cuenta SIN_FACTURA tiene siempre pagos y
    # o bien cero facturas (Σ Haber == 0), o bien un pago sin factura CONFIRMADO
    # por conciliación fina (flag PAGO_SIN_FACTURA_CONFIRMADO).
    for r in inf.resultados:
        if r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA:
            assert r.suma_debe > 0
            assert r.suma_haber == 0 or "PAGO_SIN_FACTURA_CONFIRMADO" in r.flags


@pytest.mark.skipif(not EXCEL.exists(), reason="Excel de muestra no disponible")
def test_excel_real_determinista():
    a = motor.analizar(parsear_excel(EXCEL))
    b = motor.analizar(parsear_excel(EXCEL))
    assert a.huella == b.huella
    assert [r.clasificacion for r in a.resultados] == [r.clasificacion for r in b.resultados]


@pytest.mark.skipif(not PDF.exists(), reason="PDF de muestra no disponible")
def test_pdf_real_parsea_y_marca_origen():
    libro = parsear_pdf(PDF)
    assert libro.origen == Origen.PDF
    assert len(libro.movimientos) > 0
    inf = motor.analizar(libro)
    assert "INFORME_SOBRE_PDF_DATOS_DEGRADADOS" in inf.flags_globales


@pytest.mark.skipif(not (EXCEL.exists() and PDF.exists()), reason="muestras no disponibles")
def test_excel_y_pdf_coinciden_en_alta_confianza():
    """El veredicto de alta confianza debe coincidir entre ambos orígenes."""
    inf_xl = motor.analizar(parsear_excel(EXCEL))
    inf_pdf = motor.analizar(parsear_pdf(PDF))
    sin_xl = {r.codigo_cuenta for r in inf_xl.resultados
              if r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA}
    sin_pdf = {r.codigo_cuenta for r in inf_pdf.resultados
               if r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA}
    # El PDF puede ser más conservador (NO_FIABLE), pero NO debe afirmar de más.
    assert sin_pdf.issubset(sin_xl)


@pytest.mark.skipif(not (EXCEL.exists() and PDF.exists()), reason="muestras no disponibles")
def test_pdf_reconstruye_debe_haber_desde_saldo():
    """Regresión: el PDF deriva Debe/Haber del saldo corrido (no del comentario)
    y no pierde las facturas que dejan el saldo a 0 (saldo omitido en el PDF).
    Resultado: clasificación idéntica a la del Excel y sin cuentas NO_FIABLE."""
    inf_xl = motor.analizar(parsear_excel(EXCEL))
    libro_pdf = parsear_pdf(PDF)
    inf_pdf = motor.analizar(libro_pdf)

    assert not libro_pdf.advertencias_parseo  # todos los netos cuadran
    assert inf_pdf.resumen.n_no_fiables == 0

    por_xl = {r.codigo_cuenta: r.clasificacion for r in inf_xl.resultados}
    por_pdf = {r.codigo_cuenta: r.clasificacion for r in inf_pdf.resultados}
    assert por_pdf == por_xl  # misma clasificación cuenta a cuenta


@pytest.mark.skipif(not ARTRIP.exists(), reason="ARTRIP 25-26.xlsx no disponible")
def test_artrip_integrated_no_afirma_falso_positivo_por_parciales():
    """Regresión del falso positivo real: en ARTRIP, INTEGRATED SOLUTIONS (4100024)
    tiene pagos PARCIALES (varios pagos que juntos liquidan una factura). El
    conciliador antiguo declaraba 3.344,71 € de 'pagos sin factura' cuando la
    cuenta en realidad DEBE 1.200,84 € en neto. Como está INFRAPAGADA (Σ Haber >
    Σ Debe), todos los pagos tienen factura: NO puede haber pago sin factura ->
    CONCILIADA en pagos sin factura (aparece en 'facturas sin pago', su sitio)."""
    inf = motor.analizar(parsear_excel(ARTRIP))
    r = next(x for x in inf.resultados if x.codigo_cuenta == "4100024")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" not in r.flags
    assert r.importe_sospechoso == Decimal("0.00")   # NO se afirma
    assert r.saldo_reconstruido == Decimal("-1200.84")  # saldo fiel al fichero


TOL = Decimal("0.01")


@pytest.mark.skipif(not REALES, reason="No hay ficheros reales disponibles")
@pytest.mark.parametrize("ruta", REALES, ids=lambda p: p.name)
def test_invariante_pagos_sin_factura_solo_sobrepagadas(ruta):
    """Regla de dominio: si se ha facturado MÁS de lo pagado (infrapagada), todos
    los pagos tienen factura -> esa cuenta NO puede salir a revisar/afirmada en
    'pagos sin factura'. Toda cuenta marcada aquí debe estar sobrepagada."""
    inf = motor.analizar(parsear_excel(ruta))
    marcadas = (Clasificacion.SIN_FACTURA_ALTA_CONFIANZA, Clasificacion.REVISAR)
    infractoras = [
        (r.codigo_cuenta, r.nombre_cuenta, r.suma_debe, r.suma_haber)
        for r in inf.resultados
        if r.clasificacion in marcadas and r.suma_haber > r.suma_debe + TOL
    ]
    assert not infractoras, f"{ruta.name}: infrapagadas marcadas en pagos: {infractoras}"


@pytest.mark.skipif(not REALES, reason="No hay ficheros reales disponibles")
@pytest.mark.parametrize("ruta", REALES, ids=lambda p: p.name)
def test_invariante_facturas_sin_pago_solo_infrapagadas(ruta):
    """Regla de dominio (inversa): si se ha pagado MÁS de lo facturado (sobrepagada),
    todas las facturas tienen pago -> esa cuenta NO puede salir a revisar/afirmada
    en 'facturas sin pago'. Toda cuenta marcada aquí debe estar infrapagada."""
    inf = motor_fsp.analizar(parsear_excel(ruta))
    marcadas = (Clasificacion.FACTURA_SIN_PAGO, Clasificacion.REVISAR)
    infractoras = [
        (r.codigo_cuenta, r.nombre_cuenta, r.suma_debe, r.suma_haber)
        for r in inf.resultados
        if r.clasificacion in marcadas and r.suma_debe > r.suma_haber + TOL
    ]
    assert not infractoras, f"{ruta.name}: sobrepagadas marcadas en facturas: {infractoras}"
