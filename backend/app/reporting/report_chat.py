"""Asistente del editor de informes: interpreta peticiones en lenguaje natural
con la API de OpenAI (ChatGPT) y devuelve un «patch» de configuración + una
respuesta amable.

Diseño:
  - Si NO hay OPENAI_API_KEY en el entorno, se lanza `SinClaveAPI`; la API
    responde con un aviso y el frontend usa su parser local de respaldo.
  - Se usan *structured outputs* (json_schema, strict) para que el modelo
    devuelva exactamente los campos del editor, con `null` en lo que no cambia.

La clave se lee del entorno (config.py ya carga un `.env` de la raíz del
proyecto). El código NUNCA contiene la clave.

Modelo: `gpt-4o-mini` por defecto; se puede cambiar con la variable de entorno
GESTIONA_CHAT_MODELO (p. ej. `gpt-4o` para mayor calidad).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

# Modelo por defecto (barato y rápido). Override con GESTIONA_CHAT_MODELO.
MODELO = os.getenv("GESTIONA_CHAT_MODELO", "gpt-4o-mini")

# Las mismas 4 pilas tipográficas que ofrece el panel «Diseño» del editor.
FUENTES_VALIDAS = [
    "'Plus Jakarta Sans',sans-serif",
    "'Newsreader',Georgia,serif",
    "'IBM Plex Sans',system-ui,sans-serif",
    "'Lora',Georgia,serif",
]

_CAMPOS_TEXTO = ("companyName", "reportTitle", "clientName", "period")
_CAMPOS_COLOR = ("primary", "accent")
_CAMPOS_BOOL = ("showTable", "showKpis", "showNote")


class SinClaveAPI(RuntimeError):
    """No hay OPENAI_API_KEY configurada en el entorno."""


_SISTEMA = (
    "Eres el asistente del editor de informes profesionales de «Gestiona+». "
    "El usuario, un asesor contable, te pide en lenguaje natural cambios de "
    "diseño o de contenido sobre un informe PDF de análisis del Libro Mayor. "
    "Devuelves ÚNICAMENTE los campos que hay que cambiar; el resto va en null.\n\n"
    "Campos (todos opcionales, usa null si no cambian):\n"
    "- companyName: nombre de la empresa que emite el informe.\n"
    "- reportTitle: título del informe.\n"
    "- clientName: cliente destinatario.\n"
    "- period: periodo o ejercicio (p. ej. «Ejercicio 2024»).\n"
    "- font: tipografía. SOLO uno de estos valores EXACTOS: "
    "\"'Plus Jakarta Sans',sans-serif\" (moderna, sans), "
    "\"'Newsreader',Georgia,serif\" (editorial serif), "
    "\"'IBM Plex Sans',system-ui,sans-serif\" (neutra, sans), "
    "\"'Lora',Georgia,serif\" (clásica serif).\n"
    "- primary: color principal en hexadecimal (#rrggbb).\n"
    "- accent: color de acento/texto en hexadecimal (#rrggbb).\n"
    "- showTable: mostrar la sección «Facturas sin pago» (booleano).\n"
    "- showKpis: mostrar «Resultados clave (KPIs)» (booleano).\n"
    "- showNote: mostrar «Nota metodológica» (booleano).\n"
    "- removeLogo: true SOLO si piden quitar el logo. No puedes añadir un logo; "
    "si lo piden, deja ese cambio en null y explica en «reply» que usen el botón «Subir logo».\n"
    "- pageBg: color de FONDO de las páginas del informe, en hexadecimal (#rrggbb). "
    "Úsalo cuando pidan cambiar el fondo.\n"
    "- css: reglas CSS libres para CUALQUIER otro cambio visual que no cubran los "
    "campos anteriores (tamaños de letra, márgenes, espaciados, bordes, sombras, "
    "colores de elementos concretos, ocultar o mostrar elementos, mayúsculas, "
    "alineación, interlineado…). Es tu comodín para «cambiar cualquier cosa».\n"
    "  · CADA selector DEBE empezar por «#gm-editor-root .gm-page» y CADA declaración "
    "DEBE llevar «!important» (los estilos base son inline y si no, no ganan).\n"
    "  · Ganchos dentro de .gm-page: título #pvTitle, cliente #pvClient, periodo "
    "#pvPeriod; también selectores por tipo (h1, h2, p, div).\n"
    "  · Para recolorear TODO el texto de las páginas NO basta con «.gm-page» "
    "(cada elemento lleva su propio color inline que gana): usa el selector "
    "universal, p. ej. «#gm-editor-root .gm-page *{color:#fff !important}».\n"
    "  · Ejemplo (agrandar el título y márgenes): "
    "\"#gm-editor-root .gm-page #pvTitle{font-size:60px !important} "
    "#gm-editor-root .gm-page{padding:0 !important}\".\n"
    "  · Devuelve el CSS COMPLETO que debe quedar aplicado. Si ya hay css en la "
    "configuración actual y el usuario pide algo nuevo, combínalo (mantén lo que "
    "sigue teniendo sentido y añade/ajusta lo nuevo). Para quitar personalizaciones, "
    "devuelve css con una cadena vacía no basta; devuelve el css mínimo deseado.\n\n"
    "Reglas:\n"
    "- PUEDES cambiar prácticamente cualquier aspecto del informe: si existe un "
    "campo específico (fondo, colores, textos, tipografía, secciones), úsalo; para "
    "todo lo demás, usa «css». Evita responder que «no puedes» salvo que la petición "
    "no tenga NADA que ver con el diseño del informe.\n"
    "- Interpreta la intención con naturalidad: sinónimos, nombres de color → su "
    "hex, «más formal» → serif + tonos sobrios, cambios relativos («más oscuro», "
    "«el fondo un poco más cálido») partiendo de la configuración actual que se te da.\n"
    "- Cambia SOLO lo que pida el usuario; deja el resto en null.\n"
    "- «reply»: una frase breve y cordial en español confirmando lo que has cambiado."
)


def _esquema() -> dict:
    def texto():
        return {"type": ["string", "null"]}

    def booleano():
        return {"type": ["boolean", "null"]}

    props = {
        "reply": {"type": "string"},
        "companyName": texto(),
        "reportTitle": texto(),
        "clientName": texto(),
        "period": texto(),
        "font": {"type": ["string", "null"], "enum": [None, *FUENTES_VALIDAS]},
        "primary": texto(),
        "accent": texto(),
        "showTable": booleano(),
        "showKpis": booleano(),
        "showNote": booleano(),
        "removeLogo": booleano(),
        "pageBg": texto(),
        "css": texto(),
    }
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


@lru_cache(maxsize=1)
def _cliente():
    if not os.getenv("OPENAI_API_KEY"):
        raise SinClaveAPI()
    from openai import OpenAI  # import diferido: el módulo carga aunque el SDK no esté

    return OpenAI()


def _patch_valido(datos: dict) -> dict:
    """Filtra la respuesta del modelo a un patch seguro para el editor."""
    patch: dict = {}
    for k in (*_CAMPOS_TEXTO, *_CAMPOS_COLOR):
        v = datos.get(k)
        if isinstance(v, str) and v.strip():
            patch[k] = v.strip()
    if datos.get("font") in FUENTES_VALIDAS:
        patch["font"] = datos["font"]
    for k in _CAMPOS_BOOL:
        if isinstance(datos.get(k), bool):
            patch[k] = datos[k]
    if datos.get("removeLogo") is True:
        patch["logo"] = None
    pb = datos.get("pageBg")
    if isinstance(pb, str) and pb.strip():
        patch["pageBg"] = pb.strip()
    css = datos.get("css")
    if isinstance(css, str) and css.strip():
        patch["css"] = css.strip()
    return patch


def interpretar(mensaje: str, cfg: dict) -> dict:
    """Devuelve {"reply": str, "patch": {...}}. Lanza SinClaveAPI o errores del SDK."""
    cliente = _cliente()
    contexto = json.dumps(
        {k: cfg.get(k) for k in (*_CAMPOS_TEXTO, "font", *_CAMPOS_COLOR, *_CAMPOS_BOOL)},
        ensure_ascii=False,
    )
    resp = cliente.chat.completions.create(
        model=MODELO,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": _SISTEMA},
            {
                "role": "user",
                "content": (
                    f"Configuración actual del informe:\n{contexto}\n\n"
                    f"Petición del usuario:\n{mensaje}"
                ),
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "report_patch",
                "strict": True,
                "schema": _esquema(),
            },
        },
    )
    msg = resp.choices[0].message
    # Rechazo del modelo (structured outputs): sin patch, solo mensaje.
    if getattr(msg, "refusal", None):
        return {"reply": msg.refusal, "patch": {}}
    datos = json.loads(msg.content or "{}")
    reply = (datos.get("reply") or "").strip() or "Hecho."
    return {"reply": reply, "patch": _patch_valido(datos)}
