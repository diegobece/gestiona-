"""Conciliador fino pago ⇄ factura por subconjuntos — determinista (v2).

Resuelve los PAGOS AGRUPADOS: un pago puede liquidar varias facturas a la vez
(p.ej. "FACTURAS ABRIL Y MAYO"). Para cada pago busca un SUBCONJUNTO de facturas
cuya suma coincida con su importe (±tolerancia). Si lo encuentra, esas facturas
quedan pagadas; si NINGÚN subconjunto cuadra, el pago es un HUÉRFANO = sin factura.

Casos que también automatiza (para reducir REVISAR sin falsos positivos):
  - ARRASTRE (la cuenta abre pagando): el pago anterior a la primera factura
    liquida una factura del EJERCICIO ANTERIOR. Se añade como "factura virtual"
    de ese importe, así ese pago cuadra y no se marca como sin factura.
  - ABONOS (rectificativas): al casar un pago se permite RESTAR un subconjunto de
    abonos (invoices − abonos = pago), que es exactamente lo que hace una
    rectificativa contra la factura.

También reconoce PAGOS PARCIALES / anticipos: varios pagos que juntos liquidan
una factura entera (o un pago que el emparejamiento voraz dejó suelto) se absorben
en una fase final, para no confundirlos con pagos sin factura.

Determinista y auditable (nada de LLM): aritmética exacta (subset-sum con bitset),
mismo libro -> mismo resultado. Descuadres < tolerancia se dan por conciliados.
El motor NO afirma sobre el resultado de este conciliador cuando la cuenta debe
dinero neto: ahí un pago sin casar es indistinguible de un parcial (lo decide el
detector con el tope del exceso neto).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from itertools import combinations

from ..domain.models import CERO, Movimiento, TipoMovimiento

# Descuadres por debajo de esto se consideran ruido y se dan por conciliados.
TOLERANCIA_DESCUADRE = Decimal("3.00")
# Cota de seguridad: nº máximo de abonos a combinar (2^k subconjuntos).
_MAX_ABONOS = 6


def _cents(d: Decimal) -> int:
    return int((d * 100).to_integral_value())


def conciliar_cuenta(
    movs: tuple[Movimiento, ...],
    tolerancia: Decimal = TOLERANCIA_DESCUADRE,
) -> list[Movimiento] | None:
    """Pagos huérfanos (sin factura ni grupo de facturas que los explique).

    Devuelve None solo si la cuenta no tiene facturas reales (el crédito es una
    reversión u otro apunte, no una factura): eso se deja a REVISAR. En el resto,
    devuelve la lista de pagos huérfanos (posiblemente vacía).
    """
    facturas = [_cents(m.haber) for m in movs
                if m.tipo == TipoMovimiento.FACTURA and m.haber > CERO]
    if not facturas:
        return None  # sin facturas: crédito no identificado -> REVISAR

    tol = _cents(tolerancia)

    # Arrastre: los pagos ANTERIORES a la primera factura liquidan facturas del
    # ejercicio anterior. Se añaden como "facturas virtuales" de ese importe.
    orden_prim_factura = min(
        m.orden for m in movs
        if m.tipo == TipoMovimiento.FACTURA and m.haber > CERO)
    facturas += [_cents(m.debe) for m in movs
                 if m.tipo == TipoMovimiento.PAGO and m.debe > CERO
                 and m.orden < orden_prim_factura]

    abonos = [_cents(-m.haber) for m in movs
              if m.tipo == TipoMovimiento.ABONO and m.haber < CERO]

    consumidas = [False] * len(facturas)
    abono_libre = [True] * len(abonos)
    por_importe: dict[int, list[int]] = defaultdict(list)
    for i, a in enumerate(facturas):
        por_importe[a].append(i)

    huerfanos: list[Movimiento] = []
    for m in movs:
        if m.tipo != TipoMovimiento.PAGO or m.debe <= CERO:
            continue
        objetivo = _cents(m.debe)
        if objetivo < tol:  # pago menor que la tolerancia -> descuadre, se ignora
            continue

        # Atajo: factura de importe exacto sin consumir (caso 1:1, el más común).
        exacta = next((i for i in por_importe.get(objetivo, ()) if not consumidas[i]), None)
        if exacta is not None:
            consumidas[exacta] = True
            continue
        # Subconjunto de facturas (pago agrupado / dentro de tolerancia).
        sub = _buscar_subconjunto(facturas, consumidas, objetivo, tol)
        if sub is not None:
            for i in sub:
                consumidas[i] = True
            continue
        # Con abonos: invoices − abonos = pago (la rectificativa neta la factura).
        if _casar_con_abonos(facturas, consumidas, abonos, abono_libre, objetivo, tol):
            continue
        huerfanos.append(m)

    # Pagos PARCIALES / anticipos: varios pagos que juntos liquidan una factura
    # entera, o un pago suelto que casa con una factura que el emparejamiento
    # voraz dejó libre. Se absorben: son trozos de una factura existente, no
    # pagos sin factura. Lo que no se pueda absorber sigue siendo huérfano.
    return _absorber_pagos_parciales(huerfanos, facturas, consumidas, tol)


def _absorber_pagos_parciales(
    huerfanos: list[Movimiento],
    facturas: list[int],
    consumidas: list[bool],
    tol: int,
) -> list[Movimiento]:
    """Quita de los huérfanos los pagos cuya suma (uno o varios) coincide con una
    factura aún no consumida. No inventa nada: reconoce que un pago sin casar
    puede ser un pago parcial/anticipo de una factura ya presente en la cuenta."""
    pagos = list(huerfanos)
    hubo_match = True
    while hubo_match and pagos:
        hubo_match = False
        importes = [_cents(m.debe) for m in pagos]
        libres = [False] * len(importes)
        for i in range(len(facturas)):
            if consumidas[i]:
                continue
            sub = _buscar_subconjunto(importes, libres, facturas[i], tol)
            if sub:  # uno o más pagos cuya suma == factura i (±tol)
                consumidas[i] = True
                for j in sorted(sub, reverse=True):
                    pagos.pop(j)
                hubo_match = True
                break
    return pagos


def _casar_con_abonos(facturas, consumidas, abonos, abono_libre, objetivo, tol) -> bool:
    """Intenta casar el pago permitiendo restar un subconjunto de abonos:
    subconjunto_facturas − subconjunto_abonos = objetivo. Consume ambos si casa."""
    libres = [i for i in range(len(abonos)) if abono_libre[i]][:_MAX_ABONOS]
    for k in range(1, len(libres) + 1):
        for combo in combinations(libres, k):
            objetivo2 = objetivo + sum(abonos[i] for i in combo)
            sub = _buscar_subconjunto(facturas, consumidas, objetivo2, tol)
            if sub is not None:
                for i in sub:
                    consumidas[i] = True
                for i in combo:
                    abono_libre[i] = False
                return True
    return False


def _buscar_subconjunto(
    facturas: list[int], consumidas: list[bool], objetivo: int, tol: int,
) -> list[int] | None:
    """Índices de facturas no consumidas cuya suma ≈ objetivo (±tol).

    Subset-sum por BITSET: `prefijos[k]` es un entero cuyo bit s indica que la suma
    s es alcanzable con las primeras k facturas. Los desplazamientos de enteros
    grandes van a nivel C, así que es rápido incluso con importes altos (pagos
    agrupados). Reconstruye el subconjunto recorriendo los prefijos hacia atrás."""
    disponibles = [(i, facturas[i]) for i in range(len(facturas))
                   if not consumidas[i] and facturas[i] <= objetivo + tol]
    if sum(v for _, v in disponibles) < objetivo - tol:
        return None  # ni sumándolas todas se llega: no hay subconjunto

    prefijos: list[int] = [1]  # bit 0 activo (suma 0 alcanzable con 0 facturas)
    for _, v in disponibles:
        prev = prefijos[-1]
        prefijos.append(prev | (prev << v))
    alcanzables = prefijos[-1]

    # Suma alcanzable más cercana al objetivo dentro de la tolerancia (exacta 1º).
    mejor = None
    for d in range(0, tol + 1):
        for s in (objetivo - d, objetivo + d):
            if s > 0 and (alcanzables >> s) & 1:
                mejor = s
                break
        if mejor is not None:
            break
    if mejor is None:
        return None

    # Reconstrucción: si la suma era alcanzable SIN la factura k, no se usa.
    idxs: list[int] = []
    s = mejor
    for k in range(len(disponibles), 0, -1):
        if (prefijos[k - 1] >> s) & 1:
            continue
        gi, v = disponibles[k - 1]
        idxs.append(gi)
        s -= v
    return idxs
