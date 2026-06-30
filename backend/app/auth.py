"""Autenticación: hash de contraseñas (PBKDF2, stdlib) y verificación.

Sin dependencias externas. Formato de hash: `pbkdf2_sha256$<iter>$<salt>$<dk>`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ITERACIONES = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERACIONES)
    return "pbkdf2_sha256${}${}${}".format(
        _ITERACIONES,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, almacenado: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = almacenado.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        esperado = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, esperado)
    except (ValueError, TypeError):
        return False


def autenticar(usuario: str, password: str, usuarios: dict[str, str]) -> bool:
    """True si el usuario existe y la contraseña es correcta (tiempo constante)."""
    almacenado = usuarios.get(usuario)
    if not almacenado:
        # Verificación señuelo para no filtrar si el usuario existe (timing).
        verify_password(password, "pbkdf2_sha256$1$AA==$AA==")
        return False
    return verify_password(password, almacenado)
