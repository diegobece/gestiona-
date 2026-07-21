"""Tests del razonador (capa LLM sobre REVISAR).

Se prueban las partes DETERMINISTAS sin gastar API (serialización, esquema,
validación de la respuesta, fecha de corte). La llamada real a Claude se prueba
solo si hay ANTHROPIC_API_KEY en el entorno (si no, se salta).
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from app.domain.models import Clasificacion, SubcategoriaRevisar
from app.engine import razonador
from app.engine.detector import MotorDeteccion
from app import service
from tests.factories import factura, libro, pago, reversion_pago

_motor = MotorDeteccion()


def _cuenta_revisar():
    """Cuenta con crédito NO identificado -> el motor la deja en REVISAR.

    Hay Haber (una reversión de pago) pero ninguna factura real, así que el
    conciliador no puede casar nada y el motor no afirma: CREDITO_NO_IDENTIFICADO
    (el caso real de 4100215). Es justo el tipo de cuenta que atiende el
    razonador. Ojo: una cuenta sobrepagada CON facturas no sirve de fixture —
    ahí la conciliación fina sí concluye y el motor afirma SIN_FACTURA_ALTA_CONFIANZA.
    """
    inf = _motor.analizar(libro(
        pago("4000010", 100, fecha=date(2026, 1, 10)),
        reversion_pago("4000010", 30, fecha=date(2026, 1, 12)),
        pago("4000010", 300, fecha=date(2026, 1, 20)),
    ))
    return next(r for r in inf.resultados if r.codigo_cuenta == "4000010")


def test_la_cuenta_de_prueba_esta_en_revisar():
    assert _cuenta_revisar().clasificacion == Clasificacion.REVISAR


def test_esquema_es_json_schema_estricto():
    esq = razonador._esquema()
    assert esq["additionalProperties"] is False
    # todas las propiedades son obligatorias (structured outputs estricto)
    assert set(esq["required"]) == set(esq["properties"])
    # La subcategoría es opcional: null O uno de los valores del dominio.
    # REGRESIÓN: la API rechaza con 400 tanto `null` dentro del enum como un
    # enum de strings declarado sobre `type: [string, null]`. La única forma
    # que acepta es anyOf(null, enum) — no volver a "simplificarlo".
    sub = esq["properties"]["subcategoria"]
    assert "enum" not in sub, "null dentro del enum: la API lo rechaza con 400"
    tipos = {rama.get("type") for rama in sub["anyOf"]}
    assert tipos == {"null", "string"}
    enum_sub = next(r["enum"] for r in sub["anyOf"] if r.get("type") == "string")
    assert None not in enum_sub
    for s in SubcategoriaRevisar:
        assert s.value in enum_sub


def test_serializar_cuenta_calcula_antiguedad_respecto_al_corte():
    r = _cuenta_revisar()
    datos = razonador._serializar_cuenta(r, date(2026, 1, 20))
    assert datos["codigo_cuenta"] == "4000010"
    assert datos["fecha_corte"] == "2026-01-20"
    # el primer pago (10/01) está a 10 días del corte
    primero = next(m for m in datos["movimientos"] if m["fecha"] == "2026-01-10")
    assert primero["antiguedad_dias"] == 10


def test_fecha_corte_es_el_ultimo_apunte():
    r = _cuenta_revisar()
    assert razonador.fecha_corte_de(r) == date(2026, 1, 20)


def test_sugerencia_valida_normaliza_basura():
    s = razonador._sugerencia_valida("4000010", {
        "veredicto": "INVENTADO",
        "subcategoria": "NO_EXISTE",
        "antiguedad_dias": 42.0,
        "reciente_sin_alarma": "sí",
        "motivo": "  ojo  ",
        "confianza": "SUPER",
    })
    assert s.veredicto == "INCIERTO"      # valor inválido -> INCIERTO
    assert s.subcategoria is None         # subcategoría inválida -> None
    assert s.antiguedad_dias == 42
    assert s.reciente_sin_alarma is True
    assert s.motivo == "ojo"
    assert s.confianza == "BAJA"          # confianza inválida -> BAJA


def test_razonar_revisar_sin_clave_devuelve_vacio(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    inf = _motor.analizar(libro(
        factura("4000010", 100, fecha=date(2026, 1, 10)),
        pago("4000010", 400, fecha=date(2026, 1, 20)),
    ))
    assert service.razonar_revisar(inf) == {}


def test_esquema_repaso_es_json_schema_estricto():
    esq = razonador._esquema_repaso()
    assert esq["additionalProperties"] is False
    item = esq["properties"]["revisiones"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])


def test_serializar_compacta_lleva_la_decision_del_motor():
    r = _cuenta_revisar()
    datos = razonador._serializar_compacta(r, date(2026, 1, 20))
    # El repaso necesita ver QUÉ decidió el motor y POR QUÉ para poder discrepar.
    assert datos["clasificacion_motor"] == "REVISAR"
    assert datos["motivo_motor"]
    assert len(datos["movimientos"]) == 3


def test_reparos_ignora_conformes_y_codigos_inventados():
    r = _cuenta_revisar()
    por_codigo = {r.codigo_cuenta: r}
    datos = {"revisiones": [
        {"codigo_cuenta": "4000010", "de_acuerdo": True,
         "duda": None, "confianza": "ALTA"},           # conforme -> fuera
        {"codigo_cuenta": "9999999", "de_acuerdo": False,
         "duda": "algo", "confianza": "ALTA"},         # no estaba en el lote -> fuera
        {"codigo_cuenta": "4000010", "de_acuerdo": False,
         "duda": "   ", "confianza": "ALTA"},          # sin motivo -> fuera
        {"codigo_cuenta": "4000010", "de_acuerdo": False,
         "duda": " revisar el arrastre ", "confianza": "MARCIANA"},
    ]}
    out = razonador._reparos_validos(datos, por_codigo)
    assert len(out) == 1
    assert out[0].duda == "revisar el arrastre"
    assert out[0].confianza == "BAJA"          # confianza inválida -> BAJA
    assert out[0].clasificacion_motor == "REVISAR"


def test_repasar_decididas_sin_clave_devuelve_vacio(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    inf = _motor.analizar(libro(
        factura("4000010", 100, fecha=date(2026, 1, 10)),
        pago("4000010", 100, fecha=date(2026, 1, 15)),
    ))
    assert service.repasar_decididas(inf) == []


def test_revisar_con_ia_sin_clave_no_esta_activo(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    inf = _motor.analizar(libro(
        factura("4000010", 100, fecha=date(2026, 1, 10)),
        pago("4000010", 400, fecha=date(2026, 1, 20)),
    ))
    out = service.revisar_con_ia(inf)
    assert out["activo"] is False
    assert out["analisis"] == {}


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="Sin ANTHROPIC_API_KEY: se omite la llamada real a Claude.",
)
def test_integracion_razonar_cuenta_real():
    """Llama de verdad a Claude. Solo corre si hay clave en el entorno."""
    razonador._cliente.cache_clear()  # por si un test previo cacheó sin clave
    s = razonador.razonar_cuenta(_cuenta_revisar(), date(2026, 1, 20))
    assert s.veredicto in razonador.VEREDICTOS
    assert s.confianza in razonador.CONFIANZAS
    assert s.motivo
