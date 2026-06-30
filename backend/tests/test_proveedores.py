"""Tests del matcher conservador de proveedores entre cuentas."""

from __future__ import annotations

from app.engine.proveedores import buscar_en_otras_cuentas, coincide, normalizar as N


def test_coincide_subconjunto_real():
    # Mismo proveedor con nombre más largo en otra cuenta -> coincide.
    assert coincide(N("AMAZON"), N("AMAZON EU ( ITALIA )"))
    assert coincide(N("GESTIONA"), N("GESTIONA IDEAS SL"))


def test_no_coincide_por_palabra_generica():
    # Estos NO son el mismo proveedor: el filtro no debe generar ruido.
    assert not coincide(N("EL CORTE INGLES"), N("CORTE CHINO ZHOU SL"))
    assert not coincide(N("ORIENTAL MARKET FRANCHISING S.L"), N("CHEF MARKET S.L"))


def test_nombre_generico_no_identifica():
    # Cuentas puente/genéricas se neutralizan (conjunto de tokens vacío).
    assert N("ACREEDORES VARIOS") == frozenset()
    assert not coincide(N("ACREEDORES VARIOS"), N("AMAZON"))


def test_sufijos_societarios_se_ignoran():
    assert coincide(N("FOODSAT, S.L."), N("FOODSAT"))


def test_buscar_excluye_la_propia_cuenta():
    indice = {
        "4000109": ("AMAZON EU ( ITALIA )", N("AMAZON EU ( ITALIA )")),
        "4100091": ("AMAZON", N("AMAZON")),
    }
    hits = buscar_en_otras_cuentas("4000109", "AMAZON EU ( ITALIA )", indice)
    assert hits == [("4100091", "AMAZON")]
    # Una cuenta única no encuentra a nadie.
    assert buscar_en_otras_cuentas("4000017", "FRINUS", indice) == []
