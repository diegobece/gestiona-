"""Serialización del resultado de conciliación a JSON (para la API/UI)."""

from __future__ import annotations

from .matching import nivel_confianza
from .service import ResultadoConciliacion


def _s(v):
    return None if v is None else str(v)


def resultado_a_dict(r: ResultadoConciliacion) -> dict:
    prov = {p.id: p for p in r.proveedores}
    cfg = r.cfg

    # Mapa de asignación + método/confianza por entidad (la última gana).
    asig = {}
    for a in r.asignaciones:
        asig[(a.entity_type.value, a.entity_id)] = a

    def linea_doc(d):
        a = asig.get((d[0], d[1].id))
        return {
            "id": d[1].id, "tipo": d[0],
            "proveedor": prov[d[1].proveedor_id].nombre if d[1].proveedor_id in prov else None,
            "fecha": d[1].fecha.isoformat() if d[1].fecha else None,
            "numero": getattr(d[1], "po_number", None) or getattr(d[1], "invoice_number", None),
            "net": _s(d[1].net), "total": _s(d[1].total),
            "budget_line": (a.budget_line_id.split(":", 1)[1] if a else None),
            "metodo": a.method.value if a else None,
            "confianza": _s(a.confidence) if a else None,
            "nivel_confianza": nivel_confianza(a.confidence, cfg) if a else "baja",
            "po_link": r.enlaces.get(d[1].id, (None,))[0] if d[0] == "invoice" else None,
        }

    cola = [linea_doc(("po", po)) for po in r.pos] + \
           [linea_doc(("invoice", f)) for f in r.facturas]

    reporte = [{
        "code": l.code, "description": l.description, "nivel": l.nivel,
        "department": l.department,
        "budget": _s(l.budget_bruto), "budget_neto": _s(l.budget_neto),
        "committed": _s(l.committed_bruto), "actuals": _s(l.actuals_bruto),
        "etc": _s(l.etc_bruto), "variance": _s(l.variance_bruto),
        "pct": float(l.pct_consumido), "estado": l.estado,
    } for l in r.reporte]

    anomalias = [{
        "entity_type": a.entity_type.value, "entity_id": a.entity_id,
        "tipo": a.tipo, "severidad": a.severidad.value, "detalle": a.detalle,
        "resuelta": a.resuelta,
    } for a in r.anomalias]

    return {
        "project_id": r.project_id,
        "lineas": [{"code": l.code, "description": l.description, "nivel": l.nivel.value,
                    "parent_code": l.parent_code} for l in r.lineas],
        "reporte": reporte,
        "cola": cola,
        "anomalias": anomalias,
        "config": {
            "tolerancia_importe": _s(cfg.tolerancia_importe),
            "ventana_dias": cfg.ventana_dias,
            "umbral_alta": _s(cfg.umbral_alta), "umbral_media": _s(cfg.umbral_media),
            "po_obligatoria": cfg.po_obligatoria,
        },
        "resumen": {
            "n_lineas": len(r.lineas), "n_pos": len(r.pos), "n_facturas": len(r.facturas),
            "n_anomalias": len(r.anomalias),
            "sin_asignar": sum(1 for c in cola if c["budget_line"] is None),
        },
    }
