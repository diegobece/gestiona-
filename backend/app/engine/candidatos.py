"""Sugeridor determinista de factura candidata para un pago.

ASISTENCIA al revisor, NO afirmación. Para un pago de una cuenta sin facturas
cuyo proveedor tiene facturas en otra cuenta, busca la factura más probable.

Filosofía (a petición del usuario): **suma de señales**. No se decide por una
sola variable (eso es arriesgado). La confianza sube cuantas más señales
corroboran la misma factura, y se muestran cuáles casaron, para que el humano
audite:

  - importe exacto (requisito de base)
  - fecha próxima (factura poco antes del pago)
  - mismo NIF/CIF (ignorando NIF comodín)
  - nombre del proveedor en la CUENTA de la factura
  - nombre del proveedor en el COMENTARIO (`Su Fra.: <nº> <PROVEEDOR>` ↔ pago)
  - referencia de la factura que menciona al proveedor
  - importe único en el conjunto buscado

Sin ninguna señal de identidad (solo importe+fecha) -> BAJA ("posible").
"""

from __future__ import annotations

import re
from decimal import Decimal

from ..domain.models import TOLERANCIA, Movimiento
from ..domain.resultados import FacturaCandidata
from .proveedores import normalizar

_DUMMY_NIF = re.compile(r"9{7,}")  # NIF comodín tipo A99999999 -> no identifica
_RE_SU_FRA = re.compile(r"\s*su\s*fra[.:\s]*", re.IGNORECASE)
# Palabras de relleno en comentarios de pago (no identifican al proveedor).
_PAGO_RUIDO = frozenset({
    "pago", "factura", "facturas", "recibo", "recibos", "concepto", "conceptos",
    "nomina", "finiquito", "indemnizacion", "indemn", "modelo", "mod", "resto",
    "numero", "num", "clientes", "servicios", "trimestre", "cuota",
})


# --------------------------------------------------------------------- tokens
def _tokens_comentario_factura(comentario: str | None) -> frozenset[str]:
    """Proveedor en `Su Fra.: <nº factura> <PROVEEDOR>` -> tokens del proveedor."""
    if not comentario:
        return frozenset()
    m = _RE_SU_FRA.match(comentario)
    if not m:
        return frozenset()
    resto = comentario[m.end():].strip()
    partes = resto.split(None, 1)  # [nº factura, "PROVEEDOR ..."]
    return normalizar(partes[1]) if len(partes) > 1 else frozenset()


def _tokens_comentario_pago(comentario: str | None) -> frozenset[str]:
    """Proveedor mencionado en el comentario del pago (p.ej. 'Recibo Naturgy…')."""
    return frozenset(t for t in normalizar(comentario or "") if t not in _PAGO_RUIDO)


def _tokens_ref(refs) -> frozenset[str]:
    return normalizar(f"{refs.su_factura or ''} {refs.factura or ''}")


# ------------------------------------------------------------------- NIF/CIF
def _norm_nif(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s or None


def _es_dummy_nif(nif: str | None) -> bool:
    return bool(nif and _DUMMY_NIF.search(nif))


def _nif_coincide(a: str | None, b: str | None) -> bool:
    a, b = _norm_nif(a), _norm_nif(b)
    if not a or not b or _es_dummy_nif(a) or _es_dummy_nif(b):
        return False
    core = lambda x: x[2:] if len(x) > 2 and x[:2].isalpha() else x
    return a == b or core(a) == core(b)


def _desfase(pago: Movimiento, factura: Movimiento) -> int | None:
    if pago.fecha is None or factura.fecha is None:
        return None
    return (pago.fecha - factura.fecha).days


# ------------------------------------------------------------------ scoring
# Pesos de cada señal. Las de identidad pesan; importe/fecha/único corroboran.
_PESO = {
    "nif": 2, "nombre_cuenta": 2, "nombre_comentario": 1, "referencia": 1,
    "fecha": 1, "unico": 1,
}


def _senales(pago, factura, prov_tokens, gap, unico) -> dict[str, bool]:
    inv_cuenta = normalizar(factura.nombre_cuenta)
    inv_coment = _tokens_comentario_factura(factura.comentario)
    inv_ref = _tokens_ref(factura.referencias)
    return {
        "nif": _nif_coincide(pago.referencias.nif, factura.referencias.nif),
        "nombre_cuenta": bool(prov_tokens & inv_cuenta),
        "nombre_comentario": bool(prov_tokens & inv_coment),
        "referencia": bool(prov_tokens & inv_ref),
        "fecha": gap is not None and 0 <= gap <= 7,
        "unico": unico,
    }


_ETIQUETA_SENAL = {
    "nif": "mismo NIF/CIF", "nombre_cuenta": "nombre en la cuenta",
    "nombre_comentario": "nombre en el comentario",
    "referencia": "referencia menciona proveedor", "fecha": "fecha próxima",
    "unico": "importe único",
}


def buscar_candidatas(
    pagos: list[Movimiento],
    facturas: list[Movimiento],
    prov_nombre: str,
    tolerancia: Decimal = TOLERANCIA,
) -> tuple[FacturaCandidata, ...]:
    """Una factura candidata por pago, puntuada por suma de señales."""
    prov_base = normalizar(prov_nombre)
    out: list[FacturaCandidata] = []
    for pago in sorted(pagos, key=lambda m: m.orden):
        prov_tokens = prov_base | _tokens_comentario_pago(pago.comentario)
        exactas = [f for f in facturas if abs(f.haber - pago.debe) <= tolerancia]
        # Ventana de fecha razonable (factura hasta 90 días antes / 15 después).
        viables = [
            f for f in exactas
            if _desfase(pago, f) is None or -15 <= _desfase(pago, f) <= 90
        ]
        if not viables:
            continue
        unico = len(exactas) == 1

        evaluadas = []
        for f in viables:
            gap = _desfase(pago, f)
            sen = _senales(pago, f, prov_tokens, gap, unico)
            identidad = sum(_PESO[k] for k in ("nif", "nombre_cuenta",
                            "nombre_comentario", "referencia") if sen[k])
            soporte = sum(_PESO[k] for k in ("fecha", "unico") if sen[k])
            evaluadas.append((identidad, soporte, gap, sen, f))

        # Mejor: más identidad, luego más soporte, luego menor desfase.
        identidad, soporte, gap, sen, mejor = max(
            evaluadas,
            key=lambda e: (e[0], e[1], -(abs(e[2]) if e[2] is not None else 9999)),
        )
        total = identidad + soporte
        if identidad == 0:
            conf = "BAJA"
        elif total >= 4:
            conf = "ALTA"
        else:
            conf = "MEDIA"

        casadas = [_ETIQUETA_SENAL[k] for k in
                   ("nif", "nombre_cuenta", "nombre_comentario", "referencia",
                    "fecha", "unico") if sen[k]]
        casadas.insert(0, "importe exacto")
        if gap is not None and not sen["fecha"]:
            casadas.append(f"fecha {abs(gap)} día(s)")

        es_generica = not (normalizar(mejor.nombre_cuenta) & prov_tokens)
        fuente = ("cuenta genérica (acreedores/varios)"
                  if es_generica and not normalizar(mejor.nombre_cuenta)
                  else "cuenta del proveedor")
        if conf == "BAJA":
            motivo = (f"Solo coincide importe ({pago.debe} €) y fecha próxima, sin "
                      f"señal de identidad del proveedor. Posible; revisar.")
        else:
            motivo = (f"Coinciden {len(casadas)} señales: {', '.join(casadas)}. "
                      f"Confianza {conf}.")

        out.append(FacturaCandidata(
            pago_orden=pago.orden, pago_fecha=pago.fecha, pago_importe=pago.debe,
            factura_cuenta=mejor.codigo_cuenta, factura_nombre=mejor.nombre_cuenta,
            factura_fecha=mejor.fecha, factura_importe=mejor.haber,
            factura_ref=mejor.referencias.su_factura or mejor.referencias.factura,
            dias_desfase=gap, confianza=conf, motivo=motivo, fuente=fuente,
            senales=tuple(casadas),
        ))
    return tuple(out)
