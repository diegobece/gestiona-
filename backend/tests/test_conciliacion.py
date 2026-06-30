"""Tests del núcleo de Conciliación Presupuestaria (Fase 1)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.conciliacion.matching import ConfigMatching
from app.conciliacion.service import conciliar

PRESUP = """code,description,budget_amount,level,parent_code
1000,Topsheet,15000,topsheet,
1100,Camara,10000,account,1000
1200,Iluminacion,5000,account,1000
"""
POS = """po_number,vendor,cif,date,net,tax,total,account
PO-1,CamaraRent SL,B11111111,2026-01-10,2000,420,2420,1100
PO-2,Luces SA,A22222222,2026-01-12,1000,210,1210,1200
"""
FACTS = """invoice_number,vendor,cif,date,net,tax,total,po_number,account
F-1,CamaraRent SL,B11111111,2026-02-10,1000,210,1210,PO-1,
F-9,Catering SL,B33333333,2026-02-15,500,105,605,,1200
"""


def _ficheros(tmp_path: Path):
    (tmp_path / "p.csv").write_text(PRESUP, encoding="utf-8")
    (tmp_path / "po.csv").write_text(POS, encoding="utf-8")
    (tmp_path / "f.csv").write_text(FACTS, encoding="utf-8")
    return tmp_path / "p.csv", tmp_path / "po.csv", tmp_path / "f.csv"


def _linea(r, code):
    return next(x for x in r.reporte if x.code == code)


def test_ingesta_y_reporte_cuadra_en_bruto(tmp_path):
    p, po, f = _ficheros(tmp_path)
    r = conciliar("PRJ", p, po, f)
    assert len(r.lineas) == 3 and len(r.pos) == 2 and len(r.facturas) == 2

    cam = _linea(r, "1100")
    assert cam.budget_bruto == Decimal("12100.00")     # 10000 * 1.21
    assert cam.committed_bruto == Decimal("2420.00")   # PO-1 abierta
    assert cam.actuals_bruto == Decimal("1210.00")     # F-1 heredada de PO-1
    assert cam.etc_bruto == Decimal("8470.00")         # 12100 - 2420 - 1210

    luz = _linea(r, "1200")
    assert luz.committed_bruto == Decimal("1210.00")   # PO-2
    assert luz.actuals_bruto == Decimal("605.00")      # F-9 por código de cuenta


def test_matching_determinista_factura_po():
    # F-1 trae po_number PO-1 -> enlace determinista PO_REF.
    import tempfile
    d = Path(tempfile.mkdtemp())
    p, po, f = _ficheros(d)
    r = conciliar("PRJ", p, po, f)
    assert r.enlaces["PRJ:inv:v:B11111111:F-1"][1].value == "po_ref"
    # F-9 sin PO pero con código de cuenta -> asignada a 1200.
    assert r.asign_fac["PRJ:inv:v:B33333333:F-9"] == "PRJ:1200"


def test_rollup_a_topsheet(tmp_path):
    p, po, f = _ficheros(tmp_path)
    r = conciliar("PRJ", p, po, f)
    top = _linea(r, "1000")
    # Topsheet = suma de 1100 (2420+1210) y 1200 (1210+605).
    assert top.committed_bruto == Decimal("3630.00")
    assert top.actuals_bruto == Decimal("1815.00")


def test_anomalias_duplicado_y_exceso(tmp_path):
    facts = FACTS + "F-1,CamaraRent SL,B11111111,2026-02-10,1000,210,1210,PO-1,\n"
    facts += "F-2,CamaraRent SL,B11111111,2026-03-01,5000,1050,6050,PO-1,\n"  # excede PO-1
    p, po = tmp_path / "p.csv", tmp_path / "po.csv"
    p.write_text(PRESUP, encoding="utf-8"); po.write_text(POS, encoding="utf-8")
    fp = tmp_path / "f.csv"; fp.write_text(facts, encoding="utf-8")
    r = conciliar("PRJ", p, po, fp)
    tipos = {a.tipo for a in r.anomalias}
    assert "factura_duplicada" in tipos
    assert "factura_excede_po" in tipos


def test_override_reasigna_linea(tmp_path):
    p, po, f = _ficheros(tmp_path)
    base = conciliar("PRJ", p, po, f)
    assert _linea(base, "1100").actuals_bruto == Decimal("1210.00")
    # Reasignar F-1 de 1100 a 1200.
    ov = {("invoice", "PRJ:inv:v:B11111111:F-1"): "1200"}
    r = conciliar("PRJ", p, po, f, overrides=ov)
    assert _linea(r, "1100").actuals_bruto == Decimal("0.00")
    assert _linea(r, "1200").actuals_bruto == Decimal("1210.00") + Decimal("605.00")


def test_fuzzy_factura_sin_po_por_proveedor_importe(tmp_path):
    # Factura sin po_number pero mismo proveedor/importe/fecha que PO-2.
    facts = "invoice_number,vendor,cif,date,net,tax,total,po_number,account\n"
    facts += "F-5,Luces SA,A22222222,2026-02-01,1000,210,1210,,\n"
    p, po = tmp_path / "p.csv", tmp_path / "po.csv"
    p.write_text(PRESUP, encoding="utf-8"); po.write_text(POS, encoding="utf-8")
    fp = tmp_path / "f.csv"; fp.write_text(facts, encoding="utf-8")
    r = conciliar("PRJ", p, po, fp)
    enlace = next(v for k, v in r.enlaces.items() if "F-5" in k)
    assert enlace[1].value == "vendor_amount"     # emparejada por proveedor+importe+fecha
