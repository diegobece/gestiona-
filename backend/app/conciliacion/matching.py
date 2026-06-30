"""Motor de matching en cascada (Presupuesto ⇄ PO ⇄ Factura).

Filosofía precision-first de la plataforma: primero lo DETERMINISTA (nº de PO,
código de cuenta), luego lo fuzzy (proveedor+importe+fecha) con confianza media.
La sugerencia semántica (LLM) es la Fase 3 y aquí queda como gancho explícito.

Cada asignación registra `method` y `confidence` y nunca se da por confirmada: pasa
por la revisión humana final (sección 10 del encargo).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import (
    Asignacion,
    EstadoAsignacion,
    Factura,
    LineaPresupuesto,
    MetodoAsignacion,
    OrdenCompra,
    Severidad,
    TipoEntidad,
)

UNO = Decimal("1")


@dataclass(frozen=True)
class ConfigMatching:
    """Parámetros operativos ajustables (sección 15 del encargo)."""
    tolerancia_importe: Decimal = Decimal("0.02")   # ±2 %
    ventana_dias: int = 90                            # PO precede a la factura ≤ N días
    umbral_alta: Decimal = Decimal("0.9")
    umbral_media: Decimal = Decimal("0.6")
    po_obligatoria: bool = False                      # regla "factura sin PO"


def _estado(conf: Decimal, cfg: ConfigMatching) -> EstadoAsignacion:
    # Todo pasa por revisión; "alta" se marca sugerida-fuerte, no auto-confirmada.
    return EstadoAsignacion.SUGGESTED


# ------------------------------------------------------ PO -> línea de presupuesto
def asignar_pos(pos: list[OrdenCompra], codigos_validos: set[str],
                cfg: ConfigMatching) -> list[Asignacion]:
    out = []
    for po in pos:
        if po.budget_line_id and po.budget_line_id in codigos_validos:
            out.append(Asignacion(
                TipoEntidad.PO, po.id, po.budget_line_id,
                MetodoAsignacion.ACCOUNT_CODE, UNO, _estado(UNO, cfg),
                "Código de cuenta explícito en la PO."))
    return out


# ------------------------------------------------------ Factura <-> PO (enlace)
def enlazar_facturas_po(facturas: list[Factura], pos: list[OrdenCompra],
                        cfg: ConfigMatching) -> dict[str, tuple[str, MetodoAsignacion, Decimal]]:
    """{factura_id: (po_id, metodo, confianza)}. Determinista por nº de PO;
    si falta, fuzzy por proveedor + importe (±tol) + ventana de fecha."""
    por_id = {p.id: p for p in pos}
    por_proveedor: dict[str, list[OrdenCompra]] = {}
    for p in pos:
        por_proveedor.setdefault(p.proveedor_id or "", []).append(p)

    enlaces = {}
    for f in facturas:
        # 1) Determinista: la factura ya trae un po_number válido.
        if f.po_id and f.po_id in por_id:
            enlaces[f.id] = (f.po_id, MetodoAsignacion.PO_REF, UNO)
            continue
        # 2) Fuzzy: mismo proveedor, importe dentro de tolerancia, fecha en ventana.
        candidatos = por_proveedor.get(f.proveedor_id or "", [])
        mejor, mejor_gap = None, None
        for p in candidatos:
            if not _importe_casa(f.total, p.total, cfg.tolerancia_importe):
                continue
            if not _fecha_en_ventana(p, f, cfg.ventana_dias):
                continue
            gap = abs((f.total - p.total))
            if mejor is None or gap < mejor_gap:
                mejor, mejor_gap = p, gap
        if mejor is not None:
            enlaces[f.id] = (mejor.id, MetodoAsignacion.VENDOR_AMOUNT, Decimal("0.75"))
    return enlaces


def _importe_casa(a: Decimal, b: Decimal, tol_pct: Decimal) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) <= abs(b) * tol_pct


def _fecha_en_ventana(po: OrdenCompra, f: Factura, dias: int) -> bool:
    if po.fecha is None or f.fecha is None:
        return True  # sin fechas no descartamos (lo cubre la revisión)
    delta = (f.fecha - po.fecha).days
    return 0 <= delta <= dias or -3 <= delta < 0  # PO precede; pequeño margen


# ------------------------------------------------ Factura -> línea de presupuesto
def asignar_facturas(facturas: list[Factura], pos: list[OrdenCompra],
                     codigos_validos: set[str],
                     enlaces: dict[str, tuple[str, MetodoAsignacion, Decimal]],
                     cfg: ConfigMatching) -> list[Asignacion]:
    po_linea = {p.id: p.budget_line_id for p in pos}
    out = []
    for f in facturas:
        # 1) Código de cuenta explícito en la factura.
        if f.budget_line_id and f.budget_line_id in codigos_validos:
            out.append(Asignacion(
                TipoEntidad.INVOICE, f.id, f.budget_line_id,
                MetodoAsignacion.ACCOUNT_CODE, UNO, _estado(UNO, cfg),
                "Código de cuenta explícito en la factura."))
            continue
        # 2) Heredar la línea de su PO enlazada.
        enlace = enlaces.get(f.id)
        if enlace:
            linea = po_linea.get(enlace[0])
            if linea and linea in codigos_validos:
                conf = Decimal("0.85") if enlace[1] == MetodoAsignacion.PO_REF else Decimal("0.7")
                out.append(Asignacion(
                    TipoEntidad.INVOICE, f.id, linea, MetodoAsignacion.PO_REF, conf,
                    _estado(conf, cfg),
                    f"Heredada de su PO ({enlace[1].value})."))
                continue
        # 3) Sin resolver: cola prioritaria (la Fase 3 LLM sugerirá aquí).
    return out


def nivel_confianza(conf: Decimal, cfg: ConfigMatching) -> str:
    if conf >= cfg.umbral_alta:
        return "alta"
    if conf >= cfg.umbral_media:
        return "media"
    return "baja"
