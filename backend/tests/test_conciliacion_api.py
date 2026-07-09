"""Integración de la API: subida Libro Mayor + extracto -> conciliación."""

from __future__ import annotations

import io

import pandas as pd
from fastapi.testclient import TestClient

from app.api.main import app


def _cliente_logueado() -> TestClient:
    c = TestClient(app)
    r = c.post("/login", data={"usuario": "admin", "password": "admin"})
    assert r.status_code == 200
    return c


def _mayor_xlsx() -> bytes:
    df = pd.DataFrame([
        {"CodigoCuenta": "400001", "Cuenta": "PROVEEDOR X", "FechaAsiento": "10/01/2026",
         "Asiento": "1", "Comentario": "Su Fra.: 000123 PROVEEDOR X", "Debe": 0, "Haber": 120},
        {"CodigoCuenta": "400001", "Cuenta": "PROVEEDOR X", "FechaAsiento": "15/01/2026",
         "Asiento": "2", "Comentario": "Pago factura", "Debe": 120, "Haber": 0},
    ])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _banco_xlsx() -> bytes:
    # Ficha de mayor de la cuenta de banco: Haber = salida, cruce por asiento.
    df = pd.DataFrame([
        {"Fecha asiento": "17/01/2026", "Número de asiento": "2", "Código cuenta": "5720002",
         "Comentario": "Pago factura", "Debe": 0, "Haber": 120},   # asiento 2 -> CASADO
        {"Fecha asiento": "18/01/2026", "Número de asiento": "999", "Código cuenta": "5720002",
         "Comentario": "Pago factura", "Debe": 0, "Haber": 999},   # no está -> SIN_REGISTRO
    ])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def test_upload_con_banco_produce_conciliacion():
    c = _cliente_logueado()
    r = c.post("/api/analizar", files={
        "file": ("mayor.xlsx", _mayor_xlsx(),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "banco": ("banco.xlsx", _banco_xlsx(),
                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tiene_conciliacion"] is True
    huella = data["huella"]

    rc = c.get(f"/api/informe/{huella}/conciliacion")
    assert rc.status_code == 200, rc.text
    conc = rc.json()
    assert conc["modo"] == "conciliacion_banco"
    s = conc["resumen"]
    assert s["n_salidas_banco"] == 2
    assert s["n_casados"] == 1          # la salida de 120 casa con el pago
    assert s["n_sin_registro"] == 1     # la de 999 no existe en contabilidad
    estados = sorted(l["estado"] for l in conc["lineas"])
    assert estados == ["CASADO", "SIN_REGISTRO"]


def test_upload_sin_banco_no_tiene_conciliacion():
    c = _cliente_logueado()
    r = c.post("/api/analizar", files={
        "file": ("mayor.xlsx", _mayor_xlsx(),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tiene_conciliacion"] is False
    huella = data["huella"]
    assert c.get(f"/api/informe/{huella}/conciliacion").status_code == 404
