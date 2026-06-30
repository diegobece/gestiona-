"""Reglas de validación / anomalías (sección 8 del encargo).

Fase 1: las deterministas. Cada anomalía lleva severidad y es filtrable.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .matching import ConfigMatching
from .models import (
    Anomalia,
    EstadoPO,
    Factura,
    LineaPresupuesto,
    OrdenCompra,
    Severidad,
    TipoEntidad,
)

_IVA_VALIDOS = {Decimal("0"), Decimal("0.04"), Decimal("0.10"), Decimal("0.21")}


def detectar(
    lineas: list[LineaPresupuesto],
    pos: list[OrdenCompra],
    facturas: list[Factura],
    asign_po: dict[str, str],
    asign_fac: dict[str, str],
    enlaces: dict[str, tuple],
    cfg: ConfigMatching,
    moneda_base: str = "EUR",
) -> list[Anomalia]:
    out: list[Anomalia] = []
    pos_by_id = {p.id: p for p in pos}
    presupuesto = {ln.id: ln.budget_amount_bruto for ln in lineas}

    # --- Facturas ----------------------------------------------------------
    vistas: dict[tuple, int] = defaultdict(int)
    facturadas_por_po: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))

    for f in facturas:
        po_id = enlaces.get(f.id, (None,))[0]
        # factura sin PO
        if cfg.po_obligatoria and not po_id:
            out.append(Anomalia(TipoEntidad.INVOICE, f.id, "factura_sin_po",
                                 Severidad.WARNING, "Factura sin PO asociada."))
        # duplicado (proveedor + número + importe)
        clave = (f.proveedor_id, f.invoice_number, f.total)
        vistas[clave] += 1
        if vistas[clave] > 1:
            out.append(Anomalia(TipoEntidad.INVOICE, f.id, "factura_duplicada",
                                 Severidad.CRITICAL,
                                 f"Duplicado de {f.invoice_number} ({f.total})."))
        # IVA inconsistente
        if f.tax_rate is not None and f.tax_rate not in _IVA_VALIDOS:
            out.append(Anomalia(TipoEntidad.INVOICE, f.id, "iva_inesperado",
                                 Severidad.WARNING, f"Tipo de IVA atípico: {f.tax_rate}."))
        elif f.net and f.tax:
            esperado = (f.net * (f.tax_rate or Decimal("0.21"))).quantize(Decimal("0.01"))
            if f.tax_rate and abs(f.tax - esperado) > Decimal("0.02"):
                out.append(Anomalia(TipoEntidad.INVOICE, f.id, "iva_inconsistente",
                                    Severidad.WARNING,
                                    f"IVA {f.tax} ≠ {esperado} esperado."))
        # moneda distinta a la base
        if f.moneda and f.moneda.upper() != moneda_base.upper():
            out.append(Anomalia(TipoEntidad.INVOICE, f.id, "moneda_extranjera",
                                 Severidad.INFO, f"Moneda {f.moneda} ≠ {moneda_base}."))
        # proveedor no reconocido
        if not f.proveedor_id:
            out.append(Anomalia(TipoEntidad.INVOICE, f.id, "proveedor_no_reconocido",
                                 Severidad.WARNING, "No se ha resuelto el proveedor."))
        # split coding cuya suma ≠ total
        if f.lineas:
            suma = sum((ln.importe for ln in f.lineas), Decimal("0.00"))
            if abs(suma - f.net) > Decimal("0.02") and abs(suma - f.total) > Decimal("0.02"):
                out.append(Anomalia(TipoEntidad.INVOICE, f.id, "split_descuadrado",
                                    Severidad.WARNING, f"Líneas suman {suma} ≠ documento."))
        if po_id:
            facturadas_por_po[po_id] += f.total

    # --- Facturas que exceden su PO ---------------------------------------
    for po_id, total_fact in facturadas_por_po.items():
        po = pos_by_id.get(po_id)
        if po and total_fact > po.total + Decimal("0.02"):
            out.append(Anomalia(TipoEntidad.PO, po_id, "factura_excede_po",
                                 Severidad.CRITICAL,
                                 f"Facturado {total_fact} > PO {po.total}."))

    # --- PO sin factura ----------------------------------------------------
    pos_con_factura = set(facturadas_por_po)
    for po in pos:
        if po.id not in pos_con_factura and po.estado != EstadoPO.CLOSED:
            out.append(Anomalia(TipoEntidad.PO, po.id, "po_sin_factura",
                                 Severidad.INFO, "PO abierta sin facturas imputadas."))

    # --- Gasto que supera el presupuesto de la línea -----------------------
    consumo: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for po_id, line_id in asign_po.items():
        po = pos_by_id.get(po_id)
        if po and po.estado != EstadoPO.CLOSED:
            consumo[line_id] += po.total
    fac_by_id = {f.id: f for f in facturas}
    for fac_id, line_id in asign_fac.items():
        f = fac_by_id.get(fac_id)
        if f:
            consumo[line_id] += f.total
    for line_id, gasto in consumo.items():
        ppto = presupuesto.get(line_id)
        if ppto is not None and gasto > ppto + Decimal("0.02"):
            out.append(Anomalia(TipoEntidad.PO, line_id, "sobre_presupuesto",
                                 Severidad.CRITICAL,
                                 f"Consumido {gasto} > presupuesto {ppto} de la línea."))
    return out
