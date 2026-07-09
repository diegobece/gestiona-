"""Parser del fichero del banco (Ficha de Mayor de tesorería o extracto) -> canónico.

Soporta el caso real del usuario: la **Ficha de Mayor de la cuenta de banco**
exportada del programa contable ('Fichas Mayor Pantalla'), con filas de cabecera/
título antes de la tabla y columnas Debe/Haber. También un extracto genérico con
columnas Fecha/Importe/Concepto.

Detecta la fila de cabecera automáticamente. Convención de signo para una cuenta
de tesorería: el **Haber es salida** (pago) y el Debe entrada; importe = Debe −
Haber (negativo = salida). Formato español "1.234,56" soportado; CSV autodetecta
separador.
"""

from __future__ import annotations

import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from ..domain.banco import CERO, ExtractoBanco, MovimientoBanco

_SIN_FECHA = ("fecha asiento", "fecha operacion", "fecha valor", "fecha contable",
              "fecha", "f valor", "f operacion", "date")
_SIN_ASIENTO = ("numero de asiento", "n asiento", "num asiento", "nº asiento",
                "asiento", "apunte")
_SIN_CUENTA = ("codigo cuenta", "cuenta", "codigo de cuenta")
_SIN_IMPORTE = ("importe eur", "importe €", "importe euros", "importe movimiento",
                "importe", "amount", "cantidad")
_SIN_CARGO = ("cargo", "cargos", "adeudo", "salida")
_SIN_ABONO = ("abono", "abonos", "ingreso", "entrada")
_SIN_DEBE = ("debe",)
_SIN_HABER = ("haber",)
_SIN_CONCEPTO = ("comentario", "concepto", "descripcion", "detalle", "movimiento",
                 "observaciones", "texto", "concepto ampliado")
_SIN_CONTRAP = ("contrapartida", "tercero", "beneficiario", "ordenante")
_SIN_FACTURA = ("documento", "n factura", "num factura", "numero factura",
                "nº factura", "factura", "referencia", "recibo")
_SIN_SALDO = ("saldo actual", "saldo posterior", "saldo")

# Tokens que exige una fila para considerarse cabecera de la tabla.
_TOKENS_CABECERA = set(_SIN_FECHA) | {"debe", "haber", "importe", "comentario",
                                      "concepto", "asiento", "cargo", "abono"}


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace(".", " ").replace("_", " ").replace("-", " ")
    return " ".join(s.split())


def _casa(columnas: dict[str, str], sinonimos: tuple[str, ...]) -> str | None:
    for syn in sinonimos:               # exacto primero
        if syn in columnas:
            return columnas[syn]
    for syn in sinonimos:               # luego por inclusión
        for norm, orig in columnas.items():
            if syn in norm:
                return orig
    return None


def _to_decimal(valor) -> Decimal:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return CERO
    if isinstance(valor, str):
        valor = valor.strip().replace(" ", "").replace(" ", "").replace("€", "")
        if not valor or valor in ("-", "--"):
            return CERO
        if "," in valor:  # formato español
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


def _leer_crudo(ruta: str | Path) -> pd.DataFrame:
    ext = Path(ruta).suffix.lower()
    if ext == ".csv":
        return pd.read_csv(ruta, dtype=object, sep=None, engine="python", header=None)
    return pd.read_excel(ruta, dtype=object, header=None)


def _fila_cabecera(raw: pd.DataFrame) -> int:
    """Índice de la fila que parece la cabecera: la que más tokens conocidos casa
    (fecha + algún importe/debe/haber). Escanea las primeras 20 filas."""
    mejor_i, mejor_n = 0, 0
    for i in range(min(20, len(raw))):
        celdas = [_norm(v) for v in raw.iloc[i].tolist() if not pd.isna(v)]
        hits = sum(1 for c in celdas if c in _TOKENS_CABECERA)
        tiene_fecha = any(c in _SIN_FECHA for c in celdas)
        tiene_importe = any(c in ("debe", "haber", "importe", "cargo", "abono")
                            for c in celdas)
        if tiene_fecha and tiene_importe and hits > mejor_n:
            mejor_i, mejor_n = i, hits
    return mejor_i


def parsear_banco(ruta: str | Path) -> ExtractoBanco:
    """Lee el fichero del banco (Excel/CSV) y produce el modelo canónico."""
    raw = _leer_crudo(ruta)
    raw = raw.dropna(how="all").reset_index(drop=True)
    hdr = _fila_cabecera(raw)

    encabezados = [_to_str(v) or f"col{j}" for j, v in enumerate(raw.iloc[hdr].tolist())]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = encabezados
    df = df.dropna(how="all")

    columnas = {_norm(c): c for c in df.columns}
    col_fecha = _casa(columnas, _SIN_FECHA)
    col_asiento = _casa(columnas, _SIN_ASIENTO)
    col_importe = _casa(columnas, _SIN_IMPORTE)
    # 'Importe asiento/divisa/cambio' son magnitudes sin signo: no sirven como
    # importe con signo del movimiento.
    if col_importe is not None and any(
            t in _norm(col_importe) for t in ("asiento", "divisa", "cambio")):
        col_importe = None
    col_cargo = _casa(columnas, _SIN_CARGO)
    col_abono = _casa(columnas, _SIN_ABONO)
    col_debe = _casa(columnas, _SIN_DEBE)
    col_haber = _casa(columnas, _SIN_HABER)
    col_concepto = _casa(columnas, _SIN_CONCEPTO)
    col_contrap = _casa(columnas, _SIN_CONTRAP)
    col_factura = _casa(columnas, _SIN_FACTURA)
    col_saldo = _casa(columnas, _SIN_SALDO)

    tiene_importe = any((col_importe, col_cargo, col_abono, col_debe, col_haber))
    if col_fecha is None or not tiene_importe:
        raise ValueError(
            "No se reconocen las columnas del fichero del banco. Se necesita al "
            "menos FECHA y un IMPORTE (Importe, Cargo/Abono o Debe/Haber). "
            f"Columnas detectadas: {list(df.columns)}."
        )

    advertencias: list[str] = []
    if col_asiento is None:
        advertencias.append(
            "El fichero del banco no tiene columna de nº de asiento: no se puede "
            "cruzar de forma exacta con la contabilidad."
        )
    if col_factura is None:
        advertencias.append("Sin columna de documento/factura en el banco.")

    movimientos: list[MovimientoBanco] = []
    for orden, fila in enumerate(df.itertuples(index=False)):
        f = dict(zip(df.columns, fila))

        # Importe con signo (negativo = salida). Prioridad: Debe/Haber (ficha de
        # tesorería, Haber = salida) > Cargo/Abono > columna única de importe.
        if col_debe is not None or col_haber is not None:
            debe = _to_decimal(f.get(col_debe)) if col_debe else CERO
            haber = _to_decimal(f.get(col_haber)) if col_haber else CERO
            importe = debe - haber
        elif col_cargo is not None or col_abono is not None:
            cargo = _to_decimal(f.get(col_cargo)) if col_cargo else CERO
            abono = _to_decimal(f.get(col_abono)) if col_abono else CERO
            importe = abono - abs(cargo)
        else:
            importe = _to_decimal(f.get(col_importe))

        fecha = _to_date(f.get(col_fecha))
        if fecha is None and importe == CERO:
            continue  # fila de título/total

        movimientos.append(MovimientoBanco(
            fecha=fecha,
            importe=importe,
            concepto=(_to_str(f.get(col_concepto)) or "") if col_concepto else "",
            asiento=_to_str(f.get(col_asiento)) if col_asiento else None,
            referencia=_to_str(f.get(col_factura)) if col_factura else None,
            contrapartida=_to_str(f.get(col_contrap)) if col_contrap else None,
            orden=orden,
            saldo=_to_decimal(f.get(col_saldo)) if col_saldo else None,
        ))

    if not movimientos:
        raise ValueError("El fichero del banco no contiene movimientos legibles.")

    return ExtractoBanco(
        movimientos=tuple(movimientos),
        advertencias_parseo=tuple(advertencias),
    )
