"""Detección determinista de un mismo proveedor en varias cuentas.

Sirve para rebajar falsos positivos: si una cuenta no tiene facturas (candidata
a SIN_FACTURA) pero el MISMO proveedor aparece en otra cuenta que SÍ tiene
facturas, la factura podría estar allí -> no se afirma, va a REVISAR.

El matcher es deliberadamente conservador: coincidencia por **subconjunto de
tokens distintivos**. Captura "AMAZON" ⊆ "AMAZON EU (ITALIA)" pero rechaza
coincidencias por palabras genéricas ("EL CORTE INGLES" vs "CORTE CHINO",
"ORIENTAL MARKET" vs "CHEF MARKET"), que serían ruido. Como esta señal solo
DEGRADA una afirmación a revisión, errar hacia el match es la dirección segura.
"""

from __future__ import annotations

import re
import unicodedata

# Sufijos societarios y palabras de cuenta genéricas que NO identifican proveedor.
_RUIDO = frozenset({
    "sl", "slu", "sa", "sau", "sl.", "lda", "ltd", "inc", "bv", "gmbh", "srl",
    "sc", "scp", "scl", "sccl", "sociedad", "limitada", "and", "the",
    "varios", "acreedores", "proveedores", "varias",
})


def normalizar(nombre: str) -> frozenset[str]:
    """Nombre de proveedor -> conjunto de tokens distintivos (sin acentos,
    minúsculas, sin puntuación, sin sufijos societarios, tokens de ≥3 chars)."""
    s = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode()
    tokens = re.findall(r"[a-z0-9]{3,}", s.lower())
    return frozenset(t for t in tokens if t not in _RUIDO)


def coincide(a: frozenset[str], b: frozenset[str]) -> bool:
    """¿Son el mismo proveedor? Conservador: uno de los conjuntos de tokens
    está contenido en el otro (y no vacío)."""
    if not a or not b:
        return False
    return a <= b or b <= a


def buscar_en_otras_cuentas(
    codigo: str,
    nombre: str,
    indice: dict[str, tuple],
) -> list[tuple[str, str]]:
    """Devuelve [(codigo_otra, nombre_otra)] de cuentas (con facturas) cuyo
    proveedor coincide con `nombre`, excluyendo la propia cuenta. Determinista
    (orden por código). El índice mapea codigo -> (nombre, tokens, ...)."""
    propio = normalizar(nombre)
    if not propio:
        return []
    hits = []
    for cod_otra, datos in indice.items():
        if cod_otra == codigo:
            continue
        nom_otra, toks_otra = datos[0], datos[1]
        if coincide(propio, toks_otra):
            hits.append((cod_otra, nom_otra))
    return sorted(hits)
