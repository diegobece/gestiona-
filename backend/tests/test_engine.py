"""Tests obligatorios del motor (§9). Precision-first: 0 falsos positivos."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.models import (
    AperturaCuenta,
    Clasificacion,
    Confianza,
    SubcategoriaRevisar,
)
from app.engine.detector import MotorDeteccion
from tests.factories import factura, libro, pago, reversion_pago

motor = MotorDeteccion()


def _res(informe, codigo):
    return next(r for r in informe.resultados if r.codigo_cuenta == codigo)


# --- §9.1: pagos y CERO facturas -> SIN_FACTURA_ALTA_CONFIANZA --------------
def test_pagos_sin_facturas_es_alta_confianza():
    inf = motor.analizar(libro(
        pago("4000500", 100, saldo=100),
        pago("4000500", 50, saldo=150),
    ))
    r = _res(inf, "4000500")
    assert r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
    # ALTA en general; MEDIA si además falta apertura y el pago cae en el primer
    # periodo (como aquí). Lo que NUNCA cambia es la clasificación.
    assert r.confianza in (Confianza.ALTA, Confianza.MEDIA)
    assert r.importe_sospechoso == Decimal("150.00")
    assert r.num_facturas == 0


# --- §9.2: pago AGRUPADO que suma varias facturas -> conciliado (v2) ----------
def test_pago_agrupado_se_concilia():
    # Un pago liquida tres facturas de una vez (40+60+100 = 200): el conciliador
    # por subconjuntos lo reconoce y NO lo marca como pago sin factura.
    inf = motor.analizar(libro(
        factura("4100061", 40), factura("4100061", 60), factura("4100061", 100),
        pago("4100061", 200),
    ))
    r = _res(inf, "4100061")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert r.importe_sospechoso == Decimal("0.00")


# --- §9.3: abonos negativos + pagos netos que cuadra -> 0 afirmadas ---------
def test_abonos_y_pagos_netos_no_se_afirma():
    inf = motor.analizar(libro(
        factura("4000009", 100),
        factura("4000009", 50, abono=True),  # abono -50 -> deuda neta 50
        pago("4000009", 50),
    ))
    r = _res(inf, "4000009")
    assert r.clasificacion in (Clasificacion.CONCILIADA, Clasificacion.REVISAR)
    assert r.clasificacion != Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
    assert r.num_abonos == 1


# --- §9.4: cuenta técnica 4009000 -> EXCLUIDA ------------------------------
def test_cuenta_tecnica_4009000_excluida():
    inf = motor.analizar(libro(
        pago("4009000", 8607.40, saldo=8607.40),
    ))
    r = _res(inf, "4009000")
    assert r.clasificacion == Clasificacion.EXCLUIDA
    # Sin la exclusión esto sería el mayor falso positivo del fichero.


# --- Fuera de alcance: cuentas que no son proveedor/acreedor ---------------
def test_cuenta_cliente_es_fuera_de_alcance():
    # 430xxxx (Clientes) no es proveedor/acreedor: fuera de alcance, NO excluida.
    inf = motor.analizar(libro(pago("4300000", 100, nombre="UN CLIENTE")))
    r = _res(inf, "4300000")
    assert r.clasificacion == Clasificacion.FUERA_DE_ALCANCE
    assert r.clasificacion != Clasificacion.EXCLUIDA


def test_banco_e_impuestos_fuera_de_alcance_y_contadores():
    inf = motor.analizar(libro(
        pago("4000004", 50, saldo=50),       # proveedor en alcance
        pago("5720000", 999, nombre="BANCO"),  # tesorería -> fuera de alcance
        factura("4720000", 21, nombre="HP IVA SOPORTADO"),  # impuestos -> fuera
        pago("4009000", 8607.40, saldo=8607.40),  # técnica -> EXCLUIDA
    ))
    assert _res(inf, "5720000").clasificacion == Clasificacion.FUERA_DE_ALCANCE
    assert _res(inf, "4720000").clasificacion == Clasificacion.FUERA_DE_ALCANCE
    assert _res(inf, "4009000").clasificacion == Clasificacion.EXCLUIDA
    assert inf.resumen.n_fuera_alcance == 2
    assert inf.resumen.n_excluidas == 1
    # En alcance = proveedor/acreedor reales (no técnicas, no fuera de alcance).
    assert inf.resumen.n_en_alcance == 1


def test_acreedor_411_en_alcance():
    # Ampliación a grupo 40/41: 411xxxx (acreedores, efectos a pagar) en alcance.
    inf = motor.analizar(libro(pago("4110000", 100, saldo=100, nombre="ACREEDOR X")))
    r = _res(inf, "4110000")
    assert r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA


# --- §9.5: saldo reconstruido != SaldoActual -> NO_FIABLE -------------------
def test_saldo_no_cuadra_es_no_fiable():
    # Pago 100 pero el saldo del fichero dice 999 -> no cuadra.
    inf = motor.analizar(libro(
        pago("4000011", 100, saldo=999.00),
    ))
    r = _res(inf, "4000011")
    assert r.clasificacion == Clasificacion.NO_FIABLE
    assert r.confianza == Confianza.NA


# --- §9.6: determinismo -----------------------------------------------------
def test_determinismo_mismo_libro_mismo_resultado():
    lb = libro(
        factura("4000047", 30), pago("4000047", 30),
        pago("4000500", 10),
    )
    a = motor.analizar(lb)
    b = motor.analizar(lb)
    assert a == b
    assert a.huella == b.huella


# --- Extra: aviso de saldo de apertura baja la confianza a MEDIA -----------
def test_saldo_apertura_ausente_baja_confianza():
    # Pago en enero (primer periodo), sin facturas, sin apertura.
    inf = motor.analizar(libro(
        pago("4000017", 311.76, fecha=date(2026, 1, 5), saldo=311.76),
    ))
    r = _res(inf, "4000017")
    assert r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
    assert r.confianza == Confianza.MEDIA
    assert "SALDO_APERTURA_AUSENTE" in r.flags


# --- Extra: con apertura presente, no se baja la confianza -----------------
def test_con_apertura_presente_confianza_alta():
    inf = motor.analizar(libro(
        pago("4000017", 100, fecha=date(2026, 1, 5), saldo=-50.00),
        aperturas={"4000017": AperturaCuenta(
            debe_anterior=Decimal("0.00"), haber_anterior=Decimal("150.00"))},
    ))
    r = _res(inf, "4000017")
    # saldo recon = 0(apertura -150) + 100 = -50 == saldo_reportado -> fiable
    assert r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
    assert r.confianza == Confianza.ALTA
    assert "SALDO_APERTURA_AUSENTE" not in r.flags


# --- Extra: sin pagos -> CONCILIADA (nada que comprobar) -------------------
def test_sin_pagos_es_conciliada():
    inf = motor.analizar(libro(factura("4000003", 200)))
    r = _res(inf, "4000003")
    assert r.clasificacion == Clasificacion.CONCILIADA


# ===========================================================================
# Sub-casillas de REVISAR (triage; ninguna se afirma)
# ===========================================================================
def _sub(inf, codigo):
    r = _res(inf, codigo)
    assert r.clasificacion == Clasificacion.REVISAR
    return r.subcategoria


def test_arrastre_abre_pagando_se_concilia():
    # Abre pagando (pago antes de la 1ª factura): ese pago liquida una factura del
    # año anterior. El conciliador lo trata como factura virtual -> CONCILIADA,
    # sin afirmar nada (no es pago sin factura, la factura existe en el año previo).
    inf = motor.analizar(libro(
        pago("4000001", 100), factura("4000001", 40),
    ))
    r = _res(inf, "4000001")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" not in r.flags


def test_pago_huerfano_en_cuenta_infrapagada_es_conciliada_en_pagos():
    # Cuenta que en NETO aún debe dinero (infrapagada: Σ Haber > Σ Debe). Aquí es
    # IMPOSIBLE que haya un pago sin factura: si se ha facturado más de lo pagado,
    # todos los pagos tienen factura. Por eso NO aparece en 'pagos sin factura'
    # (ni a revisar): es CONCILIADA en este análisis. Lo pendiente (la factura de
    # 300 sin pagar) se trata en el análisis inverso 'facturas sin pago'.
    inf = motor.analizar(libro(
        factura("4000014", 100), pago("4000014", 100),  # casa 1:1
        pago("4000014", 185),                           # no casa: ¿parcial?
        factura("4000014", 300),                        # queda sin pagar
    ))
    r = _res(inf, "4000014")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" not in r.flags
    assert r.importe_sospechoso == Decimal("0.00")  # no se afirma nada


def test_pago_agrupado_con_descuadre_menor_tolerancia_se_concilia():
    # Un pago agrupado con un descuadre de céntimos (< 3€) se concilia igualmente.
    inf = motor.analizar(libro(
        factura("4100145", 42.35), factura("4100145", 30.25),
        pago("4100145", 72.60),                          # 42.35+30.25 exacto
        factura("4100145", 50), pago("4100145", 51.50),  # descuadre 1.50 < 3€
    ))
    r = _res(inf, "4100145")
    assert r.clasificacion == Clasificacion.CONCILIADA


# --- Regla del saldo final: sobrepago CONFIRMADO -> SIN_FACTURA -------------
def test_sobrepago_confirmado_pasa_a_sin_factura():
    # Concilia la factura (a crédito y de vuelta a 0) y luego 2 pagos finales sin
    # factura cuyo importe == exceso: factura confirmada ausente -> alta confianza.
    inf = motor.analizar(libro(
        factura("4100200", 50), pago("4100200", 50),
        pago("4100200", 40), pago("4100200", 30),
    ))
    r = _res(inf, "4100200")
    assert r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
    assert r.confianza == Confianza.ALTA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" in r.flags
    assert r.importe_sospechoso == Decimal("70.00")


def test_sobrepago_confirmado_con_pago_huerfano_en_medio():
    # Réplica de 4100106: concilia varias facturas, un pago sin factura EN MEDIO
    # (no al final), y luego otra factura con su pago. Las facturas se cubren con
    # pagos enteros y sobra el pago huérfano = exceso -> SIN_FACTURA alta confianza.
    inf = motor.analizar(libro(
        factura("4100206", 285), pago("4100206", 285),
        pago("4100206", 261),                       # huérfano, en medio
        factura("4100206", 301), pago("4100206", 301),
    ))
    r = _res(inf, "4100206")
    assert r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" in r.flags
    assert r.importe_sospechoso == Decimal("261.00")


def test_abre_pagando_se_concilia_aunque_llegue_a_credito():
    # Réplica de FAN CLIMA: abre con un PAGO (factura del año anterior); el resto
    # opera normal. El pago inicial es arrastre (factura virtual) y el resto casa
    # 1:1 -> CONCILIADA, sin afirmar nada.
    inf = motor.analizar(libro(
        pago("4100304", 320),                       # abre pagando (arrastre)
        factura("4100304", 320), pago("4100304", 320),
        factura("4100304", 1768), pago("4100304", 1768),
    ))
    r = _res(inf, "4100304")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" not in r.flags


def test_residuo_menor_tolerancia_se_concilia():
    # Un pago cuadra su factura y sobra un residuo de 0,15 € (< 3€): se da por
    # conciliado, no se afirma ni se manda a revisar.
    inf = motor.analizar(libro(
        factura("4100303", 50), pago("4100303", 50), pago("4100303", "0.15"),
    ))
    r = _res(inf, "4100303")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" not in r.flags


def test_arrastre_multiples_pagos_apertura_se_concilia():
    # Abre con dos pagos antes de la 1ª factura (arrastre): ambos se tratan como
    # facturas virtuales del año anterior -> CONCILIADA, no se afirma.
    inf = motor.analizar(libro(
        pago("4100301", 60), pago("4100301", 40), factura("4100301", 60),
    ))
    r = _res(inf, "4100301")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" not in r.flags


def test_abono_rectificativa_neta_la_factura_se_concilia():
    # Una rectificativa (abono) reduce la factura: pago = factura − abono. El
    # conciliador lo reconoce (100 − 30 = 70) -> CONCILIADA, sin afirmar.
    inf = motor.analizar(libro(
        factura("4100302", 100), factura("4100302", 30, abono=True),
        pago("4100302", 70),
    ))
    r = _res(inf, "4100302")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" not in r.flags


def test_sub_credito_no_identificado_sin_facturas_reales():
    # Hay Haber (reversión de pago) pero ninguna factura 'Su Fra.'.
    inf = motor.analizar(libro(
        pago("4100215", 50), pago("4100215", 50), reversion_pago("4100215", 50),
    ))
    assert _sub(inf, "4100215") == SubcategoriaRevisar.CREDITO_NO_IDENTIFICADO.value


def test_abono_no_usado_no_estorba_la_conciliacion():
    # Los pagos casan 1:1 con sus facturas y sobra un abono no aplicado (un crédito
    # pendiente): eso NO es un pago sin factura -> CONCILIADA.
    inf = motor.analizar(libro(
        factura("4000009", 50), pago("4000009", 50),
        factura("4000009", 30), pago("4000009", 30),
        factura("4000009", 20, abono=True),
    ))
    r = _res(inf, "4000009")
    assert r.clasificacion == Clasificacion.CONCILIADA
    assert "PAGO_SIN_FACTURA_CONFIRMADO" not in r.flags


def test_sub_factura_en_otra_cuenta_degrada_sin_factura():
    # 4000109 solo paga (sin facturas) pero AMAZON tiene facturas en 4100091.
    inf = motor.analizar(libro(
        pago("4000109", 43.94, nombre="AMAZON EU ( ITALIA )"),
        factura("4100091", 100, nombre="AMAZON"),
        pago("4100091", 100, nombre="AMAZON"),
    ))
    r = _res(inf, "4000109")
    assert r.clasificacion == Clasificacion.REVISAR  # NO se afirma SIN_FACTURA
    assert r.subcategoria == SubcategoriaRevisar.FACTURA_EN_OTRA_CUENTA.value
    assert "4100091" in r.subcategoria_motivo


def test_factura_candidata_alta_confianza():
    # El pago de 4000109 (26,95, 15-abr) debe sugerir la factura 26,95 de 4100091
    # con confianza ALTA (importe exacto, único, factura 1 día antes).
    from datetime import date
    inf = motor.analizar(libro(
        pago("4000109", 26.95, nombre="AMAZON EU ( ITALIA )", fecha=date(2026, 4, 15)),
        factura("4100091", 26.95, nombre="AMAZON", fecha=date(2026, 4, 14)),
        factura("4100091", 99.00, nombre="AMAZON", fecha=date(2026, 4, 1)),
        pago("4100091", 99.00, nombre="AMAZON", fecha=date(2026, 4, 2)),
    ))
    r = _res(inf, "4000109")
    assert r.subcategoria == SubcategoriaRevisar.FACTURA_EN_OTRA_CUENTA.value
    assert len(r.candidatos) == 1
    cand = r.candidatos[0]
    assert cand.factura_cuenta == "4100091"
    assert cand.factura_importe == Decimal("26.95")
    assert cand.confianza == "ALTA"
    assert cand.dias_desfase == 1


def test_candidata_generica_baja_sin_identidad():
    # El 16,99 está en una cuenta genérica (ACREEDORES VARIOS, token vacío) con NIF
    # comodín -> candidata POSIBLE (BAJA), no se infla.
    from datetime import date
    inf = motor.analizar(libro(
        pago("4000109", 16.99, nombre="AMAZON EU ( ITALIA )", fecha=date(2026, 4, 8)),
        factura("4100091", 99.00, nombre="AMAZON"),  # match de nombre -> en alcance del cruce
        pago("4100091", 99.00, nombre="AMAZON"),
        factura("4100000", 16.99, nombre="ACREEDORES VARIOS", fecha=date(2026, 4, 6)),
    ))
    r = _res(inf, "4000109")
    cand = next(c for c in r.candidatos if c.pago_importe == Decimal("16.99"))
    assert cand.factura_cuenta == "4100000"
    assert cand.confianza == "BAJA"
    assert "genérica" in cand.fuente


def test_candidata_generica_sube_confianza_con_mismo_nif():
    # Si la factura en la cuenta genérica comparte NIF real con el proveedor, la
    # señal de identidad sube la confianza (suma de filtros: NIF + fecha + único).
    import itertools
    from datetime import date
    from app.domain.models import Movimiento, Origen, Referencias, TipoMovimiento
    o = itertools.count(1000)

    def mov(codigo, nombre, tipo, debe, haber, nif, fecha, com):
        return Movimiento(
            codigo_cuenta=codigo, nombre_cuenta=nombre, fecha=fecha, asiento="1",
            tipo=tipo, debe=Decimal(str(debe)), haber=Decimal(str(haber)),
            comentario=com, referencias=Referencias(nif=nif), orden=next(o),
            origen=Origen.EXCEL, saldo_reportado=None,
        )

    inf = motor.analizar(libro(
        mov("4000109", "AMAZON EU ( ITALIA )", TipoMovimiento.PAGO, 16.99, 0,
            "ESB12345678", date(2026, 4, 8), "Pago factura"),
        factura("4100091", 99.00, nombre="AMAZON"), pago("4100091", 99.00, nombre="AMAZON"),
        mov("4100000", "ACREEDORES VARIOS", TipoMovimiento.FACTURA, 0, 16.99,
            "ESB12345678", date(2026, 4, 6), "Su Fra.:  X1 ACREEDORES VARIOS"),
    ))
    r = _res(inf, "4000109")
    cand = next(c for c in r.candidatos if c.pago_importe == Decimal("16.99"))
    assert cand.confianza != "BAJA"             # el NIF la corrobora
    assert "mismo NIF/CIF" in cand.senales


def test_senal_nombre_en_comentario_caso_naturgy():
    # El proveedor del pago aparece en el comentario de la factura
    # ("Su Fra.: <nº> NATURGY IBERIA") y/o en el del pago ("Recibo Naturgy…"):
    # esa señal de nombre-en-comentario corrobora aunque la cuenta sea genérica.
    import itertools
    from datetime import date
    from app.domain.models import Movimiento, Origen, Referencias, TipoMovimiento
    o = itertools.count(2000)

    def mov(codigo, nombre, tipo, debe, haber, fecha, com):
        return Movimiento(
            codigo_cuenta=codigo, nombre_cuenta=nombre, fecha=fecha, asiento="1",
            tipo=tipo, debe=Decimal(str(debe)), haber=Decimal(str(haber)),
            comentario=com, referencias=Referencias(), orden=next(o),
            origen=Origen.EXCEL, saldo_reportado=None,
        )

    inf = motor.analizar(libro(
        # Cuenta del proveedor NATURGY: paga (sin facturas propias) con comentario descriptivo.
        mov("4100010", "NATURGY IBERIA S.A", TipoMovimiento.PAGO, 60.00,
            0, date(2026, 4, 10), "Recibo Naturgy Clientes, S.a.u."),
        # Otra cuenta del mismo proveedor con la factura.
        mov("4100011", "NATURGY IBERIA SA", TipoMovimiento.FACTURA, 0, 60.00,
            date(2026, 4, 8), "Su Fra.:  FE2639 NATURGY IBERIA"),
        mov("4100011", "NATURGY IBERIA SA", TipoMovimiento.PAGO, 999, 0,
            date(2026, 4, 9), "Pago factura"),
    ))
    r = _res(inf, "4100010")
    cand = next(c for c in r.candidatos if c.pago_importe == Decimal("60.00"))
    assert "nombre en el comentario" in cand.senales
    assert cand.confianza in ("ALTA", "MEDIA")


def test_factura_candidata_no_inventa_si_no_hay_importe():
    # Pago sin factura del mismo importe en las cuentas del proveedor -> sin candidata.
    inf = motor.analizar(libro(
        pago("4000109", 16.99, nombre="AMAZON EU ( ITALIA )"),
        factura("4100091", 99.00, nombre="AMAZON"),
        pago("4100091", 99.00, nombre="AMAZON"),
    ))
    r = _res(inf, "4000109")
    assert r.subcategoria == SubcategoriaRevisar.FACTURA_EN_OTRA_CUENTA.value
    assert r.candidatos == ()  # no hay 16,99 en las cuentas del proveedor


def test_proveedor_unico_sigue_siendo_sin_factura():
    # FRINUS no aparece en ninguna otra cuenta -> se mantiene la afirmación.
    inf = motor.analizar(libro(
        pago("4000017", 311.76, nombre="FRINUS"),
        factura("4100091", 100, nombre="AMAZON"),
        pago("4100091", 100, nombre="AMAZON"),
    ))
    r = _res(inf, "4000017")
    assert r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
    assert r.subcategoria is None


def test_subcategoria_solo_en_revisar():
    # Las cuentas que no son REVISAR no llevan sub-casilla.
    inf = motor.analizar(libro(pago("4000500", 100, saldo=100)))
    r = _res(inf, "4000500")
    assert r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
    assert r.subcategoria is None
