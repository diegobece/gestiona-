"""Tests de autenticación y protección de rutas."""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("GESTIONA_SECRET_KEY", "test-secret-key-para-tests-1234567890")
os.environ["GESTIONA_CODIGO_REGISTRO"] = "gestiona-demo"  # código fijo para los tests
# BD de usuarios aislada para los tests (no toca la real usuarios.db).
os.environ["GESTIONA_USERS_DB"] = os.path.join(
    tempfile.gettempdir(), "gestiona_test_usuarios.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.auth import hash_password, verify_password  # noqa: E402

# Cliente sin seguir redirecciones, para comprobar el 303 a /login.
cliente = TestClient(app, follow_redirects=False)


def test_hash_verifica_y_rechaza():
    h = hash_password("secreta123")
    assert verify_password("secreta123", h)
    assert not verify_password("otra", h)
    assert h != hash_password("secreta123")  # salt distinto cada vez


def test_home_sin_login_redirige():
    r = cliente.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_api_sin_login_da_401():
    r = cliente.get("/api/informe/loquesea/facturas")
    assert r.status_code == 401


def test_login_invalido():
    r = cliente.post("/login", data={"usuario": "admin", "password": "malo"})
    assert r.status_code == 401


def test_login_valido_da_acceso():
    # En dev existe admin/admin por defecto.
    c = TestClient(app)  # sigue redirecciones
    r = c.post("/login", data={"usuario": "admin", "password": "admin"})
    assert r.status_code == 200
    # Con sesión, la home ya responde 200.
    assert c.get("/").status_code == 200
    # Y logout corta el acceso.
    c.get("/logout")
    assert TestClient(app, follow_redirects=False).get("/").status_code == 303


def test_health_es_publico():
    assert cliente.get("/health").status_code == 200


def test_cabeceras_de_seguridad():
    r = cliente.get("/login")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "content-security-policy" in {k.lower() for k in r.headers}
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


# --- Registro autoservicio (con código de invitación) ----------------------
def test_registro_form_publico():
    assert cliente.get("/registro").status_code == 200


def test_registro_codigo_incorrecto_falla():
    r = cliente.post("/registro", data={
        "usuario": "nuevo1", "password": "clave1234", "password2": "clave1234",
        "codigo": "MAL"})
    assert r.status_code == 400


def test_registro_valido_crea_y_permite_login():
    import uuid
    user = "user_" + uuid.uuid4().hex[:8]
    c = TestClient(app)  # sigue redirecciones
    r = c.post("/registro", data={
        "usuario": user, "password": "clave1234", "password2": "clave1234",
        "codigo": "gestiona-demo"})  # código por defecto en dev
    assert r.status_code == 200
    assert c.get("/").status_code == 200          # queda con sesión iniciada
    # Y puede volver a entrar luego (cuenta guardada).
    c2 = TestClient(app)
    assert c2.post("/login", data={"usuario": user, "password": "clave1234"}).status_code == 200


def test_registro_password_corta_falla():
    r = cliente.post("/registro", data={
        "usuario": "nuevo2", "password": "corta", "password2": "corta",
        "codigo": "gestiona-demo"})
    assert r.status_code == 400


def test_registro_usuario_duplicado_falla():
    import uuid
    user = "dup_" + uuid.uuid4().hex[:8]
    ok = {"usuario": user, "password": "clave1234", "password2": "clave1234",
          "codigo": "gestiona-demo"}
    assert TestClient(app).post("/registro", data=ok).status_code == 200
    # Segundo intento con el mismo usuario -> 400.
    assert cliente.post("/registro", data=ok).status_code == 400
