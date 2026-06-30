"""Clasificación determinista comentario/importe -> TipoMovimiento.

Compartido por los parsers de Excel y PDF para que ambos produzcan el mismo
modelo canónico. Sin heurísticas borrosas: reglas explícitas y auditables.
"""

from __future__ import annotations

import re
from decimal import Decimal

from ..domain.models import CERO, TipoMovimiento

_RE_SU_FRA = re.compile(r"^\s*su\s*fra", re.IGNORECASE)
_RE_PAGO = re.compile(r"^\s*pago", re.IGNORECASE)


def clasificar(comentario: str, debe: Decimal, haber: Decimal) -> TipoMovimiento:
    """Determina el tipo de un apunte a partir de su comentario e importes.

    Reglas (en orden):
      1. Comentario `Su Fra.: ...` con Haber negativo -> ABONO (rectificativa).
      2. Comentario `Su Fra.: ...`                    -> FACTURA.
      3. Comentario `Pago ...`                        -> PAGO.
      4. Resto: se infiere por el lado del apunte (Haber=factura, Debe=pago),
         y si no, OTRO. Conservador: ante la duda no inventamos factura.
    """
    c = comentario or ""

    if _RE_SU_FRA.match(c):
        return TipoMovimiento.ABONO if haber < CERO else TipoMovimiento.FACTURA

    if _RE_PAGO.match(c):
        return TipoMovimiento.PAGO

    # Sin comentario reconocible: inferimos por el lado contable.
    if haber < CERO:
        return TipoMovimiento.ABONO
    if haber > CERO and debe == CERO:
        return TipoMovimiento.FACTURA
    if debe > CERO and haber == CERO:
        return TipoMovimiento.PAGO
    return TipoMovimiento.OTRO
