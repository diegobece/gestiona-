"""Integración contra los ficheros reales de muestra (si están disponibles).

Valida que el flujo completo ingesta->motor se comporta como esperamos sobre
los datos reales, y que Excel y PDF coinciden en el veredicto por cuenta.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.models import Clasificacion, Origen
from app.engine.detector import MotorDeteccion
from app.ingest.excel_parser import parsear_excel
from app.ingest.pdf_parser import parsear_pdf

DOWNLOADS = Path.home() / "Downloads"
EXCEL = DOWNLOADS / "TemporalFichasMayor_20260611_112723.xlsx"
PDF = DOWNLOADS / "Fichas de mayor configurable (vertical) 110626132546.pdf"

motor = MotorDeteccion()


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

    # Ninguna afirmación sin pagos.
    for r in inf.resultados:
        if r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA:
            assert r.suma_debe > 0 and r.suma_haber == 0


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
