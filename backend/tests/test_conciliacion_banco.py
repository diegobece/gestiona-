"""Tests del motor de conciliación banco ⇄ proveedores (cruce por asiento).

Reglas: CASADO si el asiento está en proveedores; si no, SIN_REGISTRO cuando el
concepto parece pago a proveedor y FUERA_DE_ALCANCE cuando es impuestos/comisiones/
efectivo… Filosofía §2: solo se destaca lo que parece pago a proveedor.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.banco import EstadoConciliacion, ExtractoBanco, MovimientoBanco
from app.domain.models import (
    AperturaCuenta,
    LibroMayor,
    Movimiento,
    Origen,
    Referencias,
    TipoMovimiento,
)

from app.engine.conciliacion_banco import MotorConciliacionBanco

MOTOR = MotorConciliacionBanco()


def salida(importe, *, asiento, fecha=date(2026, 1, 17), concepto="Pago factura",
           contrap=None, orden=0):
    """Un cargo del banco (Haber en tesorería): importe negativo."""
    return MovimientoBanco(
        fecha=fecha, importe=Decimal(str(-abs(importe))), concepto=concepto,
        asiento=asiento, referencia=None, contrapartida=contrap, orden=orden)


def entrada(importe, *, asiento="1", orden=0):
    return MovimientoBanco(
        fecha=date(2026, 1, 17), importe=Decimal(str(abs(importe))), concepto="COBRO",
        asiento=asiento, referencia=None, contrapartida=None, orden=orden)


def extracto(*movs):
    return ExtractoBanco(movimientos=tuple(movs))


def pago(codigo, importe, asiento, *, fecha=date(2026, 1, 15), nombre="PROVEEDOR X"):
    return Movimiento(
        codigo_cuenta=codigo, nombre_cuenta=nombre, fecha=fecha, asiento=asiento,
        tipo=TipoMovimiento.PAGO, debe=Decimal(str(importe)).quantize(Decimal("0.01")),
        haber=Decimal("0.00"), comentario="Pago factura",
        referencias=Referencias(su_factura="FRA-1"), orden=hash(asiento) % 1000,
        origen=Origen.EXCEL)


def libro(*movs):
    return LibroMayor(movimientos=tuple(movs),
                      aperturas={m.codigo_cuenta: AperturaCuenta() for m in movs},
                      origen=Origen.EXCEL)


def _estados(inf):
    return [l.estado for l in inf.lineas]


def test_casado_por_asiento():
    lib = libro(pago("400001", 120, "100"))
    inf = MOTOR.conciliar(lib, extracto(salida(120, asiento="100")))
    assert _estados(inf) == [EstadoConciliacion.CASADO]
    assert inf.lineas[0].pago_codigo_cuenta == "400001"
    assert inf.resumen.n_casados == 1


def test_sin_registro_cuando_asiento_no_esta_y_parece_pago():
    lib = libro(pago("400001", 120, "100"))
    inf = MOTOR.conciliar(lib, extracto(
        salida(999, asiento="500", concepto="Pago factura")))
    assert _estados(inf) == [EstadoConciliacion.SIN_REGISTRO]
    assert inf.resumen.importe_sin_registro == Decimal("999.00")


def test_impuestos_van_fuera_de_alcance():
    lib = libro(pago("400001", 120, "100"))
    inf = MOTOR.conciliar(lib, extracto(
        salida(11400, asiento="586", concepto="PAGO MOD 123 4T")))
    assert _estados(inf) == [EstadoConciliacion.FUERA_DE_ALCANCE]
    assert inf.lineas[0].categoria == "Impuestos / Hacienda"


def test_comisiones_y_efectivo_fuera_de_alcance():
    lib = libro(pago("400001", 120, "100"))
    inf = MOTOR.conciliar(lib, extracto(
        salida(396, asiento="573", concepto="Comisiones bancarias", orden=0),
        salida(200, asiento="572", concepto="RETIRADA EFECTIVO", orden=1)))
    assert all(e == EstadoConciliacion.FUERA_DE_ALCANCE for e in _estados(inf))
    assert inf.resumen.n_fuera_alcance == 2


def test_contrapartida_de_proveedor_es_hallazgo_aunque_concepto_generico():
    lib = libro(pago("400001", 120, "100"))
    inf = MOTOR.conciliar(lib, extracto(
        salida(300, asiento="700", concepto="TRANSFERENCIA", contrap="4000045")))
    assert _estados(inf) == [EstadoConciliacion.SIN_REGISTRO]


def test_concepto_desconocido_no_se_afirma_como_hallazgo():
    """Sin señal de proveedor ni categoría conocida -> fuera de alcance (menos ruido)."""
    lib = libro(pago("400001", 120, "100"))
    inf = MOTOR.conciliar(lib, extracto(
        salida(88, asiento="801", concepto="XYZ RARO")))
    assert _estados(inf) == [EstadoConciliacion.FUERA_DE_ALCANCE]
    assert inf.lineas[0].categoria == "Otros (sin identificar)"


def test_entradas_se_ignoran():
    lib = libro(pago("400001", 120, "100"))
    inf = MOTOR.conciliar(lib, extracto(entrada(500), salida(120, asiento="100")))
    assert len(inf.lineas) == 1
    assert inf.resumen.n_salidas_banco == 1


def test_pago_sin_salida_en_banco_es_aviso_inverso():
    lib = libro(
        pago("400001", 120, "100", fecha=date(2026, 1, 15)),   # casará
        pago("400002", 333, "888", fecha=date(2026, 1, 16)),   # sin salida en banco
    )
    inf = MOTOR.conciliar(lib, extracto(salida(120, asiento="100")))
    asientos = [p.asiento for p in inf.pagos_sin_banco]
    assert "888" in asientos
    assert inf.resumen.n_pagos_sin_banco == 1


def test_determinismo():
    lib = libro(pago("400001", 120, "100"))
    ext = extracto(salida(120, asiento="100"), salida(50, asiento="X", concepto="Pago factura"))
    a = MOTOR.conciliar(lib, ext)
    b = MOTOR.conciliar(lib, ext)
    assert _estados(a) == _estados(b)
    assert a.resumen == b.resumen
