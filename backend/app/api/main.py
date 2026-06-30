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
from ..domain.resultados import Informe
from ..persistence.store import OverrideStore
from ..reporting.excel_export import exportar_excel
from ..reporting.pdf_export import exportar_pdf, exportar_pdf_facturas
from ..reporting.serializar import informe_a_dict, informe_facturas_a_dict
from ..seguridad import configurar_seguridad
from ..service import analizar_facturas_libro, analizar_libro, parsear

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

# Sufijo para aislar la visibilidad del informe inverso de la del directo.
_FSP = "::fsp"


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.post("/api/analizar")
async def analizar(file: UploadFile = File(...)) -> JSONResponse:
    sufijo = Path(file.filename or "").suffix.lower()
    if sufijo not in {".xlsx", ".xlsm", ".xls", ".pdf"}:
        raise HTTPException(400, "Formato no soportado. Sube un Excel (.xlsx) o PDF.")
    datos = await file.read()
    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as tmp:
        tmp.write(datos)
        ruta = tmp.name
    try:
        libro = parsear(ruta)
        informe = analizar_libro(libro)          # pagos sin factura
        informe_fsp = analizar_facturas_libro(libro)  # facturas sin pago
    except Exception as e:  # parseo/validación fallida -> 422 legible
        raise HTTPException(422, f"No se pudo procesar el fichero: {e}")
    finally:
        Path(ruta).unlink(missing_ok=True)

    # Clave común para ambos análisis = huella del análisis de pagos.
    _informes[informe.huella] = informe
    _informes_fsp[informe.huella] = informe_fsp
    return JSONResponse(informe_a_dict(
        informe, _store.listar(informe.huella), _store.ocultos(informe.huella)))


@app.get("/api/informe/{huella}")
def obtener(huella: str) -> JSONResponse:
    informe = _informes.get(huella)
    if informe is None:
        raise HTTPException(404, "Informe no encontrado (vuelve a subir el fichero).")
    return JSONResponse(informe_a_dict(
        informe, _store.listar(huella), _store.ocultos(huella)))


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
    datos = exportar_pdf(informe, _store.ocultos(huella))
    return Response(
        datos,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="informe_{huella}.pdf"'},
    )


# Sirve estáticos adicionales si los hubiera.
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
