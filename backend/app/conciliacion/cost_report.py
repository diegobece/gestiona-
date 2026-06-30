"""Cost report: Budget / Committed / Actuals / ETC / Desviación.

Base de comparación por defecto: **BRUTO** (total con IVA). Se almacena también el
neto y la vista puede alternar. Rollup de detalle → account → topsheet por la
jerarquía `parent_code`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import CERO, EstadoPO, Factura, LineaPresupuesto, OrdenCompra


@dataclass(frozen=True)
class LineaReporte:
    code: str
    description: str
    nivel: str
    department: str | None
    budget_bruto: Decimal
    budget_neto: Decimal
    committed_bruto: Decimal
    committed_neto: Decimal
    actuals_bruto: Decimal
    actuals_neto: Decimal
    etc_bruto: Decimal            # Budget − Committed − Actuals (bruto)
    variance_bruto: Decimal       # Budget − (Committed + Actuals)
    pct_consumido: Decimal        # (Committed + Actuals) / Budget
    estado: str                   # dentro | cerca | sobre

    @property
    def consumido_bruto(self) -> Decimal:
        return (self.committed_bruto + self.actuals_bruto).quantize(Decimal("0.01"))


def _suma(vals) -> Decimal:
    t = CERO
    for v in vals:
        t += v
    return t.quantize(Decimal("0.01"))


def cost_report(
    lineas: list[LineaPresupuesto],
    pos: list[OrdenCompra],
    facturas: list[Factura],
    asign_po: dict[str, str],          # po_id -> budget_line_id
    asign_fac: dict[str, str],         # factura_id -> budget_line_id
    umbral_cerca: Decimal = Decimal("0.9"),
) -> list[LineaReporte]:
    por_code = {ln.code: ln for ln in lineas}
    id_a_code = {ln.id: ln.code for ln in lineas}

    # --- valores PROPIOS por línea (committed y actuals) --------------------
    comm_b: dict[str, Decimal] = {c: CERO for c in por_code}
    comm_n: dict[str, Decimal] = {c: CERO for c in por_code}
    act_b: dict[str, Decimal] = {c: CERO for c in por_code}
    act_n: dict[str, Decimal] = {c: CERO for c in por_code}

    pos_by_id = {p.id: p for p in pos}
    for po_id, line_id in asign_po.items():
        po = pos_by_id.get(po_id)
        code = id_a_code.get(line_id)
        if po is None or code is None or po.estado == EstadoPO.CLOSED:
            continue  # Committed = solo POs abiertas/parciales
        comm_b[code] += po.total
        comm_n[code] += po.net

    fac_by_id = {f.id: f for f in facturas}
    for fac_id, line_id in asign_fac.items():
        f = fac_by_id.get(fac_id)
        code = id_a_code.get(line_id)
        if f is None or code is None:
            continue
        act_b[code] += f.total      # abonos en negativo netean solos
        act_n[code] += f.net

    # --- rollup por jerarquía parent_code ----------------------------------
    hijos: dict[str | None, list[str]] = {}
    for ln in lineas:
        hijos.setdefault(ln.parent_code, []).append(ln.code)

    memo: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}

    def rolled(code: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        if code in memo:
            return memo[code]
        cb, cn, ab, an = comm_b[code], comm_n[code], act_b[code], act_n[code]
        for h in hijos.get(code, []):
            rcb, rcn, rab, ran = rolled(h)
            cb += rcb; cn += rcn; ab += rab; an += ran
        memo[code] = (cb, cn, ab, an)
        return memo[code]

    salida = []
    for ln in lineas:
        cb, cn, ab, an = rolled(ln.code)
        cb = cb.quantize(Decimal("0.01")); cn = cn.quantize(Decimal("0.01"))
        ab = ab.quantize(Decimal("0.01")); an = an.quantize(Decimal("0.01"))
        consumido = cb + ab
        etc = (ln.budget_amount_bruto - consumido).quantize(Decimal("0.01"))
        variance = etc
        pct = (consumido / ln.budget_amount_bruto) if ln.budget_amount_bruto else CERO
        pct = pct.quantize(Decimal("0.0001"))
        if consumido > ln.budget_amount_bruto:
            estado = "sobre"
        elif ln.budget_amount_bruto and pct >= umbral_cerca:
            estado = "cerca"
        else:
            estado = "dentro"
        salida.append(LineaReporte(
            code=ln.code, description=ln.description, nivel=ln.nivel.value,
            department=ln.department,
            budget_bruto=ln.budget_amount_bruto, budget_neto=ln.budget_amount,
            committed_bruto=cb, committed_neto=cn, actuals_bruto=ab, actuals_neto=an,
            etc_bruto=etc, variance_bruto=variance, pct_consumido=pct, estado=estado,
        ))
    return salida
