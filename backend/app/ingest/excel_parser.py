"""Parser de Excel (Fichas de Mayor) -> modelo canónico.

Excel es la FUENTE AUTORITATIVA. Se prioriza siempre que exista.
Valida el parseo reconstruyendo el saldo de cada cuenta contra `SaldoActual`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from ..domain.models import (
    CERO,
    AperturaCuenta,
    LibroMayor,
    Movimiento,
    Origen,
    Referencias,
)
from .clasificador import clasificar

# Columnas que el motor necesita. Si falta alguna obligatoria, fallamos pronto.
_OBLIGATORIAS = {"CodigoCuenta", "Comentario", "Debe", "Haber"}


def _to_decimal(valor) -> Decimal:
    """Convierte un valor de celda a Decimal con 2 decimales, determinista."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return CERO
    if isinstance(valor, str):
        valor = valor.strip().replace(" ", "").replace(" ", "")
        if not valor:
            return CERO
        # Formato español "1.234,56" -> "1234.56"
        if "," in valor:
            valor = valor.replace(".", "").replace(",", ".")
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return CERO


def _to_str(valor) -> str | None:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    s = str(valor).strip()
    if not s or s.lower() == "nan":
        return None
    # Enteros leídos como float ("23.0" -> "23")
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return s


def _to_date(valor) -> date | None:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    try:
        ts = pd.to_datetime(valor, errors="coerce", dayfirst=True)
        if pd.isna(ts):
            return None
        return ts.date()
    except (ValueError, TypeError):
        return None


def parsear_excel(ruta: str | Path) -> LibroMayor:
    """Lee un Libro Mayor en Excel y produce el modelo canónico."""
    df = pd.read_excel(ruta, dtype=object)
    faltan = _OBLIGATORIAS - set(df.columns)
    if faltan:
        raise ValueError(
            f"El Excel no tiene las columnas obligatorias: {sorted(faltan)}"
        )

    movimientos: list[Movimiento] = []
    aperturas: dict[str, AperturaCuenta] = {}

    for orden, fila in enumerate(df.itertuples(index=False)):
        f = fila._asdict()
        codigo = _to_str(f.get("CodigoCuenta"))
        if codigo is None:
            continue

        debe = _to_decimal(f.get("Debe"))
        haber = _to_decimal(f.get("Haber"))
        comentario = _to_str(f.get("Comentario")) or ""

        refs = Referencias(
            serie=_to_str(f.get("Serie")),
            factura=_ref(f.get("Factura")),
            documento_conta=_to_str(f.get("DocumentoConta")),
            su_factura=_ref(f.get("SuFacturaNo")),
            contrapartida=_to_str(f.get("Contrapartida")),
            tipo_factura=_to_str(f.get("TipoFactura")),
            nif=(_to_str(f.get("NIF")) or _to_str(f.get("CifEuropeo"))
                 or _to_str(f.get("CifDni"))),
        )

        mov = Movimiento(
            codigo_cuenta=codigo,
            nombre_cuenta=_to_str(f.get("Cuenta")) or "",
            fecha=_to_date(f.get("FechaAsiento")),
            asiento=_to_str(f.get("Asiento")) or "",
            tipo=clasificar(comentario, debe, haber),
            debe=debe,
            haber=haber,
            comentario=comentario,
            referencias=refs,
            orden=orden,
            origen=Origen.EXCEL,
            saldo_reportado=_saldo(f.get("SaldoActual")),
            vencimiento=_to_date(f.get("FechaVencimiento")),
        )
        movimientos.append(mov)

        if codigo not in aperturas:
            aperturas[codigo] = AperturaCuenta(
                debe_anterior=_to_decimal(f.get("DebeSumasAnteriores")),
                haber_anterior=_to_decimal(f.get("HaberSumasAnteriores")),
            )

    return LibroMayor(
        movimientos=tuple(movimientos),
        aperturas=aperturas,
        origen=Origen.EXCEL,
    )


def _ref(valor) -> str | None:
    """Referencia de documento; '0' significa 'sin documento' (p.ej. en pagos)."""
    s = _to_str(valor)
    return None if s in (None, "0", "0.0") else s


def _saldo(valor) -> Decimal | None:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    return _to_decimal(valor)
