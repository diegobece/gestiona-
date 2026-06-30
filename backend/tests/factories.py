"""Constructores de movimientos canónicos para los tests del motor."""

from __future__ import annotations

import itertools
from datetime import date
from decimal import Decimal

from app.domain.models import (
    AperturaCuenta,
    LibroMayor,
    Movimiento,
    Origen,
    Referencias,
)
from app.ingest.clasificador import clasificar

_orden = itertools.count()


def factura(codigo, importe, *, nombre="PROVEEDOR X", fecha=date(2026, 1, 10),
            saldo=None, abono=False):
    haber = Decimal(str(importe))
    if abono:
        haber = -abs(haber)
    com = "Su Fra.:  000123 PROVEEDOR X"
    return Movimiento(
        codigo_cuenta=codigo, nombre_cuenta=nombre, fecha=fecha, asiento="1",
        tipo=clasificar(com, Decimal("0.00"), haber),
        debe=Decimal("0.00"), haber=haber.quantize(Decimal("0.01")),
        comentario=com, referencias=Referencias(), orden=next(_orden),
        origen=Origen.EXCEL, saldo_reportado=_d(saldo),
    )


def pago(codigo, importe, *, nombre="PROVEEDOR X", fecha=date(2026, 1, 15), saldo=None):
    debe = Decimal(str(importe))
    com = "Pago factura"
    return Movimiento(
        codigo_cuenta=codigo, nombre_cuenta=nombre, fecha=fecha, asiento="2",
        tipo=clasificar(com, debe, Decimal("0.00")),
        debe=debe.quantize(Decimal("0.01")), haber=Decimal("0.00"),
        comentario=com, referencias=Referencias(), orden=next(_orden),
        origen=Origen.EXCEL, saldo_reportado=_d(saldo),
    )


def reversion_pago(codigo, importe, *, nombre="PROVEEDOR X", fecha=date(2026, 1, 16)):
    """Un 'Pago factura' contabilizado en el Haber (reversión): crea crédito que
    NO es una factura. Reproduce el caso real de 4100215."""
    haber = Decimal(str(importe))
    com = "Pago factura"
    return Movimiento(
        codigo_cuenta=codigo, nombre_cuenta=nombre, fecha=fecha, asiento="3",
        tipo=clasificar(com, Decimal("0.00"), haber),
        debe=Decimal("0.00"), haber=haber.quantize(Decimal("0.01")),
        comentario=com, referencias=Referencias(), orden=next(_orden),
        origen=Origen.EXCEL, saldo_reportado=None,
    )


def libro(*movs, aperturas=None, origen=Origen.EXCEL):
    codigos = {m.codigo_cuenta for m in movs}
    aps = {c: AperturaCuenta() for c in codigos}
    if aperturas:
        aps.update(aperturas)
    return LibroMayor(movimientos=tuple(movs), aperturas=aps, origen=origen)


def _d(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.01"))
