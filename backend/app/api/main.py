"""API FastAPI: carga de fichero -> análisis -> informe + overrides + exports.

UI fina: la inteligencia está en el motor. Esta capa solo orquesta I/O.
El informe se mantiene en memoria por 'huella' (hash del libro) para servir el
drill-down, los exports y recuperar overrides persistidos del mismo fichero.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config
from ..domain.banco import InformeConciliacion
from ..domain.resultados import Informe
from ..persistence.store import OverrideStore
from ..reporting.excel_export import exportar_excel
from ..reporting.pdf_export import exportar_pdf, exportar_pdf_facturas
from ..reporting.report_chat import SinClaveAPI, interpretar as interpretar_chat
from ..reporting.serializar import (
    conciliacion_banco_a_dict,
    informe_a_dict,
    informe_facturas_a_dict,
)
from ..seguridad import configurar_seguridad
from ..service import (
    analizar_facturas_libro,
    analizar_libro,
    conciliar_banco,
    parsear,
    parsear_extracto_banco,
    revisar_con_ia,
)

app = FastAPI(title="Detección de pagos sin factura", version="1.0",
              docs_url=None, redoc_url=None, openapi_url=None)

# Login obligatorio + cabeceras de seguridad + rutas /login,/logout,/health.
configurar_seguridad(app)

# Pestaña independiente: Conciliación Presupuestaria (Presupuesto ⇄ PO ⇄ Factura).
from .conciliacion_api import router as conciliacion_router  # noqa: E402
app.include_router(conciliacion_router)
for _aviso in config.AVISOS_ARRANQUE:
    print(f"[seguridad] {_aviso}")

_STATIC = Path(__file__).parent.parent / "static"
_store = OverrideStore(Path(__file__).parent.parent.parent / "overrides.db")

# Cache en memoria por huella (del análisis de pagos, usada como clave común).
_informes: dict[str, Informe] = {}        # análisis de pagos sin factura
_informes_fsp: dict[str, Informe] = {}    # análisis inverso: facturas sin pago
_conciliaciones: dict[str, InformeConciliacion] = {}  # cruce banco ⇄ contabilidad

# Sufijo para aislar la visibilidad del informe inverso de la del directo.
_FSP = "::fsp"


@app.get("/", response_class=HTMLResponse)
def landing() -> HTMLResponse:
    """Página pública de inicio (landing). No requiere sesión."""
    return HTMLResponse((_STATIC / "landing.html").read_text(encoding="utf-8"))


@app.get("/app", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Aplicación (protegida por login)."""
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/informe", response_class=HTMLResponse)
def editor_informe() -> HTMLResponse:
    """Editor de informe de cliente (protegido por login)."""
    return HTMLResponse((_STATIC / "informe.html").read_text(encoding="utf-8"))


class ChatInformeIn(BaseModel):
    message: str
    cfg: dict = {}


@app.post("/api/informe/chat")
def informe_chat(body: ChatInformeIn) -> JSONResponse:
    """Interpreta una petición del asistente del editor con el modelo de chat
    (OpenAI) y devuelve un patch de configuración. Si no hay clave de API o
    falla, responde ok:false para que el frontend use su parser local de respaldo."""
    mensaje = (body.message or "").strip()
    if not mensaje:
        return JSONResponse({"ok": False, "reason": "vacio"})
    try:
        resultado = interpretar_chat(mensaje, body.cfg or {})
    except SinClaveAPI:
        return JSONResponse({"ok": False, "reason": "sin_api"})
    except Exception as e:  # cuota, red, respuesta no válida… -> respaldo local
        return JSONResponse({"ok": False, "reason": "error", "detail": str(e)[:200]})
    return JSONResponse({"ok": True, **resultado})


async def _volcar_temporal(archivo: UploadFile) -> str:
    """Guarda el UploadFile en un temporal y devuelve la ruta."""
    sufijo = Path(archivo.filename or "").suffix.lower()
    datos = await archivo.read()
    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as tmp:
        tmp.write(datos)
        return tmp.name


@app.post("/api/analizar")
async def analizar(
    file: UploadFile = File(...),
    banco: UploadFile | None = File(None),
) -> JSONResponse:
    sufijo = Path(file.filename or "").suffix.lower()
    if sufijo not in {".xlsx", ".xlsm", ".xls", ".pdf"}:
        raise HTTPException(400, "Formato no soportado. Sube un Excel (.xlsx) o PDF.")
    ruta = await _volcar_temporal(file)
    try:
        libro = parsear(ruta)
        informe = analizar_libro(libro)          # pagos sin factura
        informe_fsp = analizar_facturas_libro(libro)  # facturas sin pago
    except Exception as e:  # parseo/validación fallida -> 422 legible
        raise HTTPException(422, f"No se pudo procesar el fichero: {e}")
    finally:
        Path(ruta).unlink(missing_ok=True)

    # Clave común para todos los análisis = huella del análisis de pagos.
    _informes[informe.huella] = informe
    _informes_fsp[informe.huella] = informe_fsp

    # Extracto bancario opcional: si viene, se concilia contra los pagos.
    tiene_conciliacion = False
    aviso_banco: str | None = None
    _conciliaciones.pop(informe.huella, None)  # limpia un cruce previo del mismo libro
    if banco is not None and (banco.filename or "").strip():
        ruta_b = await _volcar_temporal(banco)
        try:
            extracto = parsear_extracto_banco(ruta_b)
            _conciliaciones[informe.huella] = conciliar_banco(libro, extracto)
            tiene_conciliacion = True
        except Exception as e:  # el fallo del banco NO tumba el análisis principal
            aviso_banco = f"No se pudo procesar el extracto bancario: {e}"
        finally:
            Path(ruta_b).unlink(missing_ok=True)

    payload = informe_a_dict(
        informe, _store.listar(informe.huella), _store.ocultos(informe.huella),
        _store.visibilidad(informe.huella))
    payload["tiene_conciliacion"] = tiene_conciliacion
    if aviso_banco:
        payload["aviso_banco"] = aviso_banco
    return JSONResponse(payload)


def _sugerencia_a_dict(s) -> dict:
    return {
        "codigo_cuenta": s.codigo_cuenta,
        "veredicto": s.veredicto,
        "subcategoria": s.subcategoria,
        "antiguedad_dias": s.antiguedad_dias,
        "reciente_sin_alarma": s.reciente_sin_alarma,
        "motivo": s.motivo,
        "confianza": s.confianza,
        "dar_por_bueno": s.dar_por_bueno,
    }


def _reparo_a_dict(r) -> dict:
    return {
        "codigo_cuenta": r.codigo_cuenta,
        "clasificacion_motor": r.clasificacion_motor,
        "duda": r.duda,
        "confianza": r.confianza,
    }


@app.post("/api/informe/{huella}/revisar-ia")
def revisar_ia(huella: str) -> JSONResponse:
    """Segunda opinión de Claude sobre un análisis ya hecho (bajo demanda).

    NO reclasifica nada: el motor determinista sigue siendo la fuente de verdad.
    Devuelve, por cada uno de los dos análisis (pagos sin factura / facturas sin
    pago):
      - `profundo`: razonamiento por cuenta sobre las que están en REVISAR.
      - `reparos` : cuentas ya decididas donde el razonador discrepa del motor.

    Sin ANTHROPIC_API_KEY responde `activo: false` y el frontend no muestra nada.
    """
    informe = _informes.get(huella)
    if informe is None:
        raise HTTPException(404, "Análisis no encontrado (vuelve a subir el fichero).")
    resultado = revisar_con_ia(informe, _informes_fsp.get(huella))
    if not resultado.get("activo"):
        return JSONResponse({"activo": False, "motivo": "sin_api"})

    analisis = {
        nombre: {
            "profundo": [_sugerencia_a_dict(s) for s in bloque["profundo"].values()],
            "reparos": [_reparo_a_dict(r) for r in bloque["reparos"]],
        }
        for nombre, bloque in resultado["analisis"].items()
    }
    total = sum(
        len(b["profundo"]) + len(b["reparos"]) for b in analisis.values()
    )
    return JSONResponse({"activo": True, "analisis": analisis, "total": total})


@app.get("/api/informe/{huella}/conciliacion")
def obtener_conciliacion(huella: str) -> JSONResponse:
    """Cruce banco ⇄ contabilidad, si se subió extracto para este libro."""
    inf = _conciliaciones.get(huella)
    if inf is None:
        raise HTTPException(404, "No hay conciliación bancaria para este análisis "
                                 "(vuelve a subir el libro y el extracto).")
    return JSONResponse(conciliacion_banco_a_dict(inf))


@app.get("/api/informe/{huella}")
def obtener(huella: str) -> JSONResponse:
    informe = _informes.get(huella)
    if informe is None:
        raise HTTPException(404, "Informe no encontrado (vuelve a subir el fichero).")
    return JSONResponse(informe_a_dict(
        informe, _store.listar(huella), _store.ocultos(huella),
        _store.visibilidad(huella)))


@app.get("/api/informe/{huella}/facturas")
def obtener_facturas(huella: str) -> JSONResponse:
    """Análisis inverso: facturas sin pago, para el mismo fichero."""
    informe = _informes_fsp.get(huella)
    if informe is None:
        raise HTTPException(404, "Informe no encontrado (vuelve a subir el fichero).")
    return JSONResponse(informe_facturas_a_dict(informe, _store.visibilidad(huella + _FSP)))


@app.get("/api/informe/{huella}/facturas/export.pdf")
def export_pdf_facturas(huella: str) -> Response:
    informe = _informes_fsp.get(huella)
    if informe is None:
        raise HTTPException(404, "Informe no encontrado.")
    datos = exportar_pdf_facturas(informe, _store.visibilidad(huella + _FSP))
    return Response(datos, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="facturas_sin_pago_{huella}.pdf"'})


class OverrideIn(BaseModel):
    veredicto: str
    nota: str | None = None
    autor: str | None = None


@app.post("/api/informe/{huella}/cuenta/{codigo}/override")
def guardar_override(huella: str, codigo: str, body: OverrideIn) -> JSONResponse:
    if huella not in _informes:
        raise HTTPException(404, "Informe no encontrado.")
    try:
        ov = _store.guardar(huella, codigo, body.veredicto, body.nota, body.autor)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({
        "ok": True,
        "override": {"codigo_cuenta": ov.codigo_cuenta, "veredicto": ov.veredicto,
                     "nota": ov.nota, "autor": ov.autor, "creado_en": ov.creado_en},
    })


class VisibilidadIn(BaseModel):
    mostrar: bool


@app.post("/api/informe/{huella}/cuenta/{codigo}/visibilidad")
def set_visibilidad(huella: str, codigo: str, body: VisibilidadIn) -> JSONResponse:
    """Decide si un pago sin factura se muestra (True) u oculta (False) en el PDF."""
    if huella not in _informes:
        raise HTTPException(404, "Informe no encontrado.")
    _store.set_visibilidad(huella, codigo, body.mostrar)
    return JSONResponse({"ok": True, "codigo_cuenta": codigo, "mostrar": body.mostrar})


@app.post("/api/informe/{huella}/factura/{codigo}/visibilidad")
def set_visibilidad_factura(huella: str, codigo: str, body: VisibilidadIn) -> JSONResponse:
    """Visibilidad en el informe PDF de facturas sin pago (namespace aislado)."""
    if huella not in _informes_fsp:
        raise HTTPException(404, "Informe no encontrado.")
    _store.set_visibilidad(huella + _FSP, codigo, body.mostrar)
    return JSONResponse({"ok": True, "codigo_cuenta": codigo, "mostrar": body.mostrar})


@app.get("/api/informe/{huella}/export.xlsx")
def export_excel(huella: str) -> Response:
    informe = _informes.get(huella)
    if informe is None:
        raise HTTPException(404, "Informe no encontrado.")
    datos = exportar_excel(informe)
    return Response(
        datos,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="informe_{huella}.xlsx"'},
    )


@app.get("/api/informe/{huella}/export.pdf")
def export_pdf(huella: str) -> Response:
    informe = _informes.get(huella)
    if informe is None:
        raise HTTPException(404, "Informe no encontrado.")
    datos = exportar_pdf(informe, _store.ocultos(huella), _store.visibilidad(huella))
    return Response(
        datos,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="informe_{huella}.pdf"'},
    )


# Sirve estáticos adicionales si los hubiera.
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
