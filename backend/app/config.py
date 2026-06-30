"""Configuración y secretos de la app (vía variables de entorno).

En producción se exige SECRET_KEY y al menos un usuario. En desarrollo local se
generan valores efímeros con aviso, para que arranque sin configurar nada.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

_RAIZ = Path(__file__).parent.parent.parent  # carpeta del proyecto


def _cargar_dotenv() -> None:
    """Carga variables del fichero .env (si existe) sin pisar las ya definidas."""
    ruta = _RAIZ / ".env"
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


_cargar_dotenv()

ENTORNO = os.getenv("GESTIONA_ENV", "dev").lower()
PRODUCCION = ENTORNO in ("production", "prod")

# Clave para firmar las cookies de sesión. En producción DEBE venir del entorno
# (si no, las sesiones se invalidan en cada reinicio).
SECRET_KEY = os.getenv("GESTIONA_SECRET_KEY") or secrets.token_hex(32)

# Duración de la sesión (horas) e inactividad.
SESSION_HORAS = int(os.getenv("GESTIONA_SESSION_HORAS", "8"))
SESSION_MAX_AGE = SESSION_HORAS * 3600

# Cookie segura (solo HTTPS) y dominio detrás de Cloudflare.
COOKIE_SEGURA = PRODUCCION or os.getenv("GESTIONA_COOKIE_SEGURA", "0") == "1"

# Registro autoservicio con CÓDIGO DE INVITACIÓN ÚNICO y siempre válido.
# Es el MISMO en local y en producción. Para cambiarlo, define la variable de
# entorno GESTIONA_CODIGO_REGISTRO; si no, se usa el de abajo.
CODIGO_REGISTRO = (os.getenv("GESTIONA_CODIGO_REGISTRO") or "gestiona2026").strip()
REGISTRO_HABILITADO = bool(CODIGO_REGISTRO)


def cargar_usuarios() -> dict[str, str]:
    """Usuarios autorizados: {usuario: hash_pbkdf2}.

    Fuentes (en orden): fichero `users.json` en la raíz, o las variables
    GESTIONA_USER / GESTIONA_PASSWORD_HASH para un único usuario.
    """
    ruta = _RAIZ / "users.json"
    if ruta.exists():
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            if isinstance(datos, dict) and datos:
                return {str(k): str(v) for k, v in datos.items()}
        except (ValueError, OSError):
            pass
    usuario = os.getenv("GESTIONA_USER")
    phash = os.getenv("GESTIONA_PASSWORD_HASH")
    if usuario and phash:
        return {usuario: phash}
    return {}


USUARIOS = cargar_usuarios()

# Aviso/parada según el entorno.
AVISOS_ARRANQUE: list[str] = []
if PRODUCCION:
    if not os.getenv("GESTIONA_SECRET_KEY"):
        raise RuntimeError(
            "Producción sin GESTIONA_SECRET_KEY. Define la variable de entorno "
            "con una clave secreta larga (p.ej. `python -c \"import secrets;"
            "print(secrets.token_hex(32))\"`)."
        )
    if not USUARIOS and not REGISTRO_HABILITADO:
        raise RuntimeError(
            "Producción sin usuarios ni registro. Habilita el registro con "
            "GESTIONA_CODIGO_REGISTRO o crea usuarios con `python crear_usuario.py`."
        )
else:
    if not os.getenv("GESTIONA_SECRET_KEY"):
        AVISOS_ARRANQUE.append(
            "DEV: SECRET_KEY efímera (las sesiones se pierden al reiniciar)."
        )
    if not USUARIOS:
        # Usuario por defecto SOLO en desarrollo, con aviso ruidoso.
        from .auth import hash_password  # import diferido para evitar ciclo
        USUARIOS = {"admin": hash_password("admin")}
        AVISOS_ARRANQUE.append(
            "DEV: usuario por defecto admin/admin. NO usar en producción."
        )
if not PRODUCCION:
    AVISOS_ARRANQUE.append(
        f"Código de invitación para registrarse: '{CODIGO_REGISTRO}'."
    )
elif CODIGO_REGISTRO == "gestiona2026":
    AVISOS_ARRANQUE.append(
        "AVISO: estás usando el código de invitación por defecto en producción. "
        "Cámbialo con GESTIONA_CODIGO_REGISTRO."
    )
