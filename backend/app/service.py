"""Orquestación: ingesta -> motor -> informe. Capa fina sobre la librería pura.

Regla de origen (§4 / §8): si hay Excel, se usa el Excel (fuente autoritativa).
El PDF solo se usa como fallback. La inteligencia vive en el motor; esto solo
elige el parser y junta el resultado con los overrides persistidos.
"""

from __future__ import annotations

from pathlib import Path

from .domain.banco import ExtractoBanco, InformeConciliacion
from .domain.models import LibroMayor
from .domain.resultados import Informe
from .engine.conciliacion_banco import MotorConciliacionBanco
from .engine.detector import MotorDeteccion
from .engine.facturas import MotorFacturasSinPago
from .ingest.banco_parser import parsear_banco
from .ingest.excel_parser import parsear_excel
from .ingest.pdf_parser import parsear_pdf

_EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}
_PDF_EXT = {".pdf"}
_BANCO_EXT = {".xlsx", ".xlsm", ".xls", ".csv"}

_motor = MotorDeteccion()
_motor_facturas = MotorFacturasSinPago()
_motor_conciliacion = MotorConciliacionBanco()


def parsear(ruta: str | Path) -> LibroMayor:
    """Elige el parser por extensión. Excel siempre que sea posible."""
    ext = Path(ruta).suffix.lower()
    if ext in _EXCEL_EXT:
        return parsear_excel(ruta)
    if ext in _PDF_EXT:
        return parsear_pdf(ruta)
    raise ValueError(f"Formato no soportado: {ext}. Use Excel (.xlsx) o PDF.")


def analizar_fichero(ruta: str | Path) -> Informe:
    """Pipeline completo para un fichero en disco."""
    return _motor.analizar(parsear(ruta))


def analizar_libro(libro: LibroMayor) -> Informe:
    return _motor.analizar(libro)


def analizar_facturas_libro(libro: LibroMayor) -> Informe:
    """Análisis inverso: facturas sin pago."""
    return _motor_facturas.analizar(libro)


def parsear_extracto_banco(ruta: str | Path) -> ExtractoBanco:
    """Parsea un extracto bancario (Excel o CSV)."""
    ext = Path(ruta).suffix.lower()
    if ext not in _BANCO_EXT:
        raise ValueError(
            f"Formato de extracto no soportado: {ext}. Use Excel (.xlsx) o CSV."
        )
    return parsear_banco(ruta)


def conciliar_banco(libro: LibroMayor, extracto: ExtractoBanco) -> InformeConciliacion:
    """Cruza el extracto bancario con los pagos registrados en el Libro Mayor."""
    return _motor_conciliacion.conciliar(libro, extracto)
