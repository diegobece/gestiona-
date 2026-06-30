"""Tests del análisis inverso: facturas sin pago (espejo precision-first)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.models import Clasificacion, Confianza
from app.engine.facturas import MotorFacturasSinPago
from tests.factories import factura, libro, pago

motor = MotorFacturasSinPago()


def _res(inf, codigo):
    return next(r for r in inf.resultados if r.codigo_cuenta == codigo)


def test_facturas_sin_ningun_pago_es_alta():
    inf = motor.analizar(libro(
        factura("4000003", 100), factura("4000003", 50),
    ))
    r = _res(inf, "4000003")
    assert r.clasificacion == Clasificacion.FACTURA_SIN_PAGO
    assert r.confianza == Confianza.ALTA
    assert r.importe_pendiente_pago == Decimal("150.00")
    assert len(r.facturas) == 2


def test_infrapagada_va_a_revisar_con_razon():
    # Tiene facturas (200) y un pago parcial (120) -> infrapagada -> REVISAR.
    inf = motor.analizar(libro(
        factura("4100061", 120), factura("4100061", 80), pago("4100061", 120),
    ))
    r = _res(inf, "4100061")
    assert r.clasificacion == Clasificacion.REVISAR
    assert r.importe_pendiente_pago == Decimal("80.00")
    assert r.subcategoria in ("DESFASE_DE_CORTE", "PAGO_PARCIAL",
                              "DEUDA_ANTIGUA", "DISTORSION_POR_ABONO")
    assert r.subcategoria_motivo  # siempre con su explicación
    assert len(r.facturas) == 2   # detalle informativo de todas las facturas


def test_infrapagada_con_abono_es_distorsion():
    inf = motor.analizar(libro(
        factura("4000009", 200), factura("4000009", 50, abono=True),
        pago("4000009", 60),
    ))
    r = _res(inf, "4000009")
    assert r.clasificacion == Clasificacion.REVISAR
    assert r.subcategoria == "DISTORSION_POR_ABONO"


def test_inclusion_pdf_default_y_override():
    from app.domain.models import incluir_en_informe_facturas as inc
    # FACTURA_SIN_PAGO: por defecto SÍ; REVISAR: por defecto NO.
    assert inc(Clasificacion.FACTURA_SIN_PAGO, None) is True
    assert inc(Clasificacion.REVISAR, None) is False
    # Overrides explícitos:
    assert inc(Clasificacion.FACTURA_SIN_PAGO, False) is False  # ocultar
    assert inc(Clasificacion.REVISAR, True) is True             # añadir al revisar
    assert inc(Clasificacion.CONCILIADA, True) is False         # nunca


def test_pdf_facturas_incluye_revisar_solo_si_se_anade():
    from app.reporting.pdf_export import exportar_pdf_facturas
    inf = motor.analizar(libro(
        factura("4000003", 100),                 # FACTURA_SIN_PAGO (default sí)
        factura("4100061", 200), pago("4100061", 120),  # REVISAR (default no)
    ))
    base = exportar_pdf_facturas(inf, {})                       # solo FSP
    con_revisar = exportar_pdf_facturas(inf, {"4100061": True})  # + la infrapagada
    assert base[:4] == b"%PDF" and con_revisar[:4] == b"%PDF"
    assert len(con_revisar) > len(base)


def test_pagada_es_conciliada():
    inf = motor.analizar(libro(factura("4000047", 100), pago("4000047", 100)))
    assert _res(inf, "4000047").clasificacion == Clasificacion.CONCILIADA


def test_solo_pagos_es_conciliada():
    # Sin facturas -> nada que comprobar en el análisis inverso.
    inf = motor.analizar(libro(pago("4000500", 100, saldo=100)))
    assert _res(inf, "4000500").clasificacion == Clasificacion.CONCILIADA


def test_tecnica_y_fuera_de_alcance():
    inf = motor.analizar(libro(
        factura("4009000", 500),                 # técnica -> EXCLUIDA
        factura("4300000", 99, nombre="CLIENTE"),  # cliente -> FUERA_DE_ALCANCE
    ))
    assert _res(inf, "4009000").clasificacion == Clasificacion.EXCLUIDA
    assert _res(inf, "4300000").clasificacion == Clasificacion.FUERA_DE_ALCANCE


def test_antiguedad_por_fecha_de_corte_determinista():
    # Corte = última fecha del libro. Antigüedad = corte - fecha factura.
    inf = motor.analizar(libro(
        factura("4000003", 100, fecha=date(2026, 1, 1)),
        factura("4000003", 50, fecha=date(2026, 3, 2)),  # corte
    ))
    r = _res(inf, "4000003")
    antig = {f.fecha: f.antiguedad_dias for f in r.facturas}
    assert antig[date(2026, 1, 1)] == 60   # 1-ene -> 2-mar = 60 días
    assert antig[date(2026, 3, 2)] == 0


def test_determinismo_mismo_libro():
    lb = libro(factura("4000003", 100), pago("4000003", 40))
    assert motor.analizar(lb).huella == motor.analizar(lb).huella
    assert [r.clasificacion for r in motor.analizar(lb).resultados] == \
           [r.clasificacion for r in motor.analizar(lb).resultados]
