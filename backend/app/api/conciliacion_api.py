"""API de la pestaña Conciliación Presupuestaria (independiente, tras login).

Sube las 3 carpetas/ficheros, corre el pipeline determinista y sirve árbol +
cola + cost report + anomalías. Las reasignaciones de la revisión humana se
persisten y quedan en audit_log.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..conciliacion.matching import ConfigMatching
from ..conciliacion.serializar import resultado_a_dict
from ..conciliacion.service import conciliar
from ..conciliacion.store import store

router = APIRouter()
_STATIC = Path(__file__).parent.parent / "static"

# Estado por proyecto: ficheros guardados + configuración (en memoria).
_proyectos: dict[str, dict] = {}


@router.get("/conciliacion", response_class=HTMLResponse)
def pagina():
    return HTMLResponse((_STATIC / "conciliacion.html").read_text(encoding="utf-8"))


def _guardar(tmp: Path, archivo: UploadFile | None) -> Path | None:
    if archivo is None or not archivo.filename:
        return None
    destino = tmp / archivo.filename
    destino.write_bytes(archivo.file.read())
    return destino


def _cfg(d: dict | None) -> ConfigMatching:
    d = d or {}
    base = ConfigMatching()
    from decimal import Decimal
    return ConfigMatching(
        tolerancia_importe=Decimal(str(d.get("tolerancia_importe", base.tolerancia_importe))),
        ventana_dias=int(d.get("ventana_dias", base.ventana_dias)),
        umbral_alta=Decimal(str(d.get("umbral_alta", base.umbral_alta))),
        umbral_media=Decimal(str(d.get("umbral_media", base.umbral_media))),
        po_obligatoria=bool(d.get("po_obligatoria", base.po_obligatoria)),
    )


def _ejecutar(pid: str) -> dict:
    est = _proyectos[pid]
    res = conciliar(pid, est["presupuesto"], est.get("pos"), est.get("facturas"),
                    cfg=est["cfg"], overrides=store.overrides(pid))
    return resultado_a_dict(res)


@router.post("/api/conciliacion/analizar")
async def analizar(
    request: Request,
    project_id: str = Form("PRJ"),
    presupuesto: UploadFile = File(...),
    pos: UploadFile | None = File(None),
    facturas: UploadFile | None = File(None),
    po_obligatoria: bool = Form(False),
    tolerancia_importe: str = Form("0.02"),
    ventana_dias: int = Form(90),
) -> JSONResponse:
    tmp = Path(tempfile.mkdtemp(prefix=f"concil_{project_id}_"))
    try:
        p_pre = _guardar(tmp, presupuesto)
        p_po = _guardar(tmp, pos)
        p_fac = _guardar(tmp, facturas)
        cfg = _cfg({"po_obligatoria": po_obligatoria,
                    "tolerancia_importe": tolerancia_importe, "ventana_dias": ventana_dias})
        _proyectos[project_id] = {"presupuesto": p_pre, "pos": p_po,
                                  "facturas": p_fac, "cfg": cfg, "tmp": tmp}
        datos = _ejecutar(project_id)
    except Exception as e:
        raise HTTPException(422, f"No se pudo conciliar: {e}")
    store.audit(project_id, request.session.get("user", "?"), "analizar",
                "project", project_id, after={"n": datos["resumen"]})
    return JSONResponse(datos)


@router.get("/api/conciliacion/{pid}")
def obtener(pid: str) -> JSONResponse:
    if pid not in _proyectos:
        raise HTTPException(404, "Proyecto no encontrado (vuelve a subir los ficheros).")
    return JSONResponse(_ejecutar(pid))


class Reasignacion(BaseModel):
    entity_type: str   # po | invoice
    entity_id: str
    code: str          # código de línea presupuestaria destino


@router.post("/api/conciliacion/{pid}/reasignar")
def reasignar(pid: str, body: Reasignacion, request: Request) -> JSONResponse:
    if pid not in _proyectos:
        raise HTTPException(404, "Proyecto no encontrado.")
    antes = store.overrides(pid).get((body.entity_type, body.entity_id))
    store.set_override(pid, body.entity_type, body.entity_id, body.code)
    store.audit(pid, request.session.get("user", "?"), "reasignar",
                body.entity_type, body.entity_id, before={"code": antes},
                after={"code": body.code})
    return JSONResponse(_ejecutar(pid))


@router.get("/api/conciliacion/{pid}/auditoria")
def auditoria(pid: str) -> JSONResponse:
    return JSONResponse({"historial": store.historial(pid)})
