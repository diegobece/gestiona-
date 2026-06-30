"""Parser de PDF (Fichas de Mayor vertical) -> modelo canónico.

FALLBACK FRÁGIL. Solo se usa si no hay Excel. El PDF colapsa Debe/Haber en una
sola columna de importe + un saldo corrido; inferimos el lado por el comentario
y dejamos que el motor VALIDE el parseo reconstruyendo el saldo (un parseo malo
acaba en NO_FIABLE, nunca en una afirmación).

Además validamos cada cuenta contra su línea "Suma Movimientos": si el neto
parseado no cuadra con el neto del PDF, se registra una advertencia.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..domain.models import (
    CERO,
    TOLERANCIA,
    AperturaCuenta,
    LibroMayor,
    Movimiento,
    Origen,
    Referencias,
)
from .clasificador import clasificar

# Token monetario en formato español: 1.234,56 / -5,74 / 311,76
_MONEY = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d{2}")
_HEADER = re.compile(r"^(4\d{6})\s+(.+?)\s*$")
_MOV = re.compile(r"^(\d{2}-\d{2}-\d{4})\s+(\d+)\s+(.*)$")
_SUMA = re.compile(r"^Suma Movimientos\b.*$")


def _money(token: str) -> Decimal:
    return Decimal(token.replace(".", "").replace(",", ".")).quantize(Decimal("0.01"))


def _fecha(token: str) -> date | None:
    try:
        d, m, y = token.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def parsear_pdf(ruta: str | Path) -> LibroMayor:
    with pdfplumber.open(ruta) as pdf:
        lineas = []
        for page in pdf.pages:
            texto = page.extract_text() or ""
            lineas.extend(texto.split("\n"))

    movimientos: list[Movimiento] = []
    advertencias: list[str] = []
    orden = 0

    codigo_actual: str | None = None
    nombre_actual: str = ""
    debe_acum = CERO
    haber_acum = CERO
    saldo_prev = CERO  # saldo de la línea anterior (apertura = 0)
    suma_pdf: tuple[Decimal, Decimal, Decimal] | None = None

    def cerrar_cuenta():
        """Valida el neto parseado contra la línea Suma Movimientos del PDF."""
        if codigo_actual is None or suma_pdf is None:
            return
        neto_parseado = debe_acum - haber_acum
        neto_pdf = suma_pdf[2]
        if abs(neto_parseado - neto_pdf) > TOLERANCIA:
            advertencias.append(
                f"Cuenta {codigo_actual}: el neto parseado ({neto_parseado} €) no "
                f"cuadra con 'Suma Movimientos' del PDF ({neto_pdf} €). "
                f"Datos degradados; se recomienda usar el Excel."
            )

    for linea in lineas:
        linea = linea.rstrip()

        # Cabecera de cuenta (puede repetirse por página).
        m_head = _HEADER.match(linea)
        if m_head and not _MOV.match(linea):
            nuevo_codigo = m_head.group(1)
            if nuevo_codigo != codigo_actual:
                cerrar_cuenta()
                codigo_actual = nuevo_codigo
                nombre_actual = m_head.group(2).strip()
                debe_acum = CERO
                haber_acum = CERO
                saldo_prev = CERO
                suma_pdf = None
            continue

        # Línea de totales de la cuenta.
        if _SUMA.match(linea):
            nums = _MONEY.findall(linea)
            if nums:
                vals = [_money(n) for n in nums]
                # El último número es el neto (Debe - Haber), siempre presente.
                neto = vals[-1]
                debe = vals[0] if len(vals) >= 2 else CERO
                haber = vals[1] if len(vals) >= 3 else CERO
                suma_pdf = (debe, haber, neto)
            continue

        # Línea de movimiento.
        m_mov = _MOV.match(linea)
        if m_mov and codigo_actual is not None:
            fecha = _fecha(m_mov.group(1))
            asiento = m_mov.group(2)
            resto = m_mov.group(3)
            nums = _MONEY.findall(resto)
            if not nums:
                continue  # no es una línea de movimiento
            if len(nums) >= 2:
                importe = _money(nums[-2])
                saldo = _money(nums[-1])
                tok_importe = nums[-2]
            else:
                # El PDF omite el saldo cuando queda exactamente 0,00: la línea
                # trae solo el importe. (P.ej. una factura que cancela el saldo.)
                importe = _money(nums[-1])
                saldo = CERO
                tok_importe = nums[-1]
            # Comentario = todo lo anterior al token del importe.
            idx = resto.find(tok_importe)
            comentario = resto[:idx].strip()

            # El SALDO corrido es la única señal fiable del PDF: la variación
            # respecto a la línea anterior es exactamente (Debe - Haber) de este
            # apunte. El comentario NO basta para el lado (en el PDF hay líneas
            # "Pago factura" que reducen el saldo: son reversiones en el Haber).
            delta = (saldo - saldo_prev).quantize(Decimal("0.01"))
            es_factura = comentario.lower().lstrip().startswith("su fra")
            if es_factura:
                # Lado Haber. Para una factura normal delta<0 -> haber>0; para un
                # abono delta>0 -> haber<0 (Haber negativo, como en el Excel).
                debe, haber = CERO, (-delta).quantize(Decimal("0.01"))
            elif delta >= CERO:
                debe, haber = delta, CERO
            else:
                debe, haber = CERO, (-delta).quantize(Decimal("0.01"))
            tipo = clasificar(comentario, debe, haber)

            saldo_prev = saldo
            debe_acum += debe
            haber_acum += haber

            movimientos.append(
                Movimiento(
                    codigo_cuenta=codigo_actual,
                    nombre_cuenta=nombre_actual,
                    fecha=fecha,
                    asiento=asiento,
                    tipo=tipo,
                    debe=debe,
                    haber=haber,
                    comentario=comentario,
                    referencias=Referencias(),
                    orden=orden,
                    origen=Origen.PDF,
                    saldo_reportado=saldo,
                )
            )
            orden += 1

    cerrar_cuenta()

    # El PDF no trae saldo de apertura fiable -> aperturas a 0.
    aperturas = {m.codigo_cuenta: AperturaCuenta() for m in movimientos}
    return LibroMayor(
        movimientos=tuple(movimientos),
        aperturas=aperturas,
        origen=Origen.PDF,
        advertencias_parseo=tuple(advertencias),
    )
