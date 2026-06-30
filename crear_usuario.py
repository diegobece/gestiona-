"""Crea o actualiza un usuario de la plataforma (lo guarda con contraseña cifrada).

Uso:
    python crear_usuario.py

Pide usuario y contraseña y los guarda en la base de datos de usuarios
(`usuarios.db`), la misma que usa el registro de la web. La contraseña NUNCA se
guarda en claro: solo su hash PBKDF2.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "backend"))
from app.auth import hash_password          # noqa: E402
from app.usuarios import store, usuario_valido  # noqa: E402


def main() -> None:
    print("== Crear/actualizar usuario de Gestiona más ==")
    usuario = input("Usuario: ").strip()
    if not usuario_valido(usuario):
        print("Usuario no válido (3–32 caracteres: letras, números, . _ - @). Cancelado.")
        return
    p1 = getpass.getpass("Contraseña: ")
    p2 = getpass.getpass("Repite la contraseña: ")
    if p1 != p2:
        print("Las contraseñas no coinciden. Cancelado.")
        return
    if len(p1) < 8:
        print("La contraseña debe tener al menos 8 caracteres. Cancelado.")
        return

    store.crear_o_actualizar(usuario, hash_password(p1))
    print(f"\nOK. Usuario '{usuario}' guardado.")
    print(f"Usuarios actuales: {', '.join(store.listar())}")


if __name__ == "__main__":
    main()
