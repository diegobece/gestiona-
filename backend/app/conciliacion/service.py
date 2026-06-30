"""Orquestación del pipeline de conciliación (puro, determinista).

ingesta → matching → anomalías → cost report. Acepta `overrides` de la revisión
humana (reasignaciones de línea), que reemplazan la asignación automática y se
reflejan en el informe. La trazabilidad (audit_log) la gestiona la capa API.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import anomalias as anom
from . import ingest
from . import matching
from .cost_report import LineaReporte, cost_report
from .matching import ConfigMatching
from .models import (
    Anomalia,
    Asignacion,
    EstadoAsignacion,
    Factura,
    LineaPresupuesto,
    MetodoAsignacion,
    OrdenCompra,
    Proveedor,
    TipoEntidad,
)


@dataclass
class ResultadoConciliacion:
    project_id: str
    lineas: list[LineaPresupuesto]
    proveedores: list[Proveedor]
    pos: list[OrdenCompra]
    facturas: list[Factura]
    asignaciones: list[Asignacion]
    asign_po: dict[str, str]               # po_id -> budget_line_id
    asign_fac: dict[str, str]              # factura_id -> budget_line_id
    enlaces: dict[str, tuple]              # factura_id -> (po_id, metodo, conf)
    anomalias: list[Anomalia]
    reporte: list[LineaReporte]
    cfg: ConfigMatching = field(default_factory=ConfigMatching)


def conciliar(
    project_id: str,
    ruta_presupuesto,
    ruta_pos,
    ruta_facturas,
    cfg: ConfigMatching | None = None,
    mapeos: dict | None = None,
    overrides: dict[tuple[str, str], str] | None = None,
) -> ResultadoConciliacion:
    cfg = cfg or ConfigMatching()
    mapeos = mapeos or {}
    overrides = overrides or {}

    lineas = ingest.parsear_presupuesto(ruta_presupuesto, project_id, mapeos.get("presupuesto"))
    registro = ingest.RegistroProveedores()
    pos = ingest.parsear_pos(ruta_pos, project_id, registro, mapeos.get("pos")) if ruta_pos else []
    facturas = ingest.parsear_facturas(ruta_facturas, project_id, registro, mapeos.get("facturas")) if ruta_facturas else []
    proveedores = registro.todos()

    codigos = {ln.id for ln in lineas}

    asig_po_list = matching.asignar_pos(pos, codigos, cfg)
    enlaces = matching.enlazar_facturas_po(facturas, pos, cfg)
    asig_fac_list = matching.asignar_facturas(facturas, pos, codigos, enlaces, cfg)
    asignaciones = asig_po_list + asig_fac_list

    asign_po = {a.entity_id: a.budget_line_id for a in asig_po_list}
    asign_fac = {a.entity_id: a.budget_line_id for a in asig_fac_list}

    # --- Overrides de la revisión humana (reasignación de línea) -----------
    for (tipo, eid), code in overrides.items():
        line_id = f"{project_id}:{code}"
        if line_id not in codigos:
            continue
        destino = asign_po if tipo == "po" else asign_fac
        destino[eid] = line_id
        asignaciones.append(Asignacion(
            TipoEntidad(tipo), eid, line_id, MetodoAsignacion.MANUAL,
            __import__("decimal").Decimal("1"), EstadoAsignacion.CONFIRMED,
            "Reasignación manual (revisión)."))

    anomalias = anom.detectar(lineas, pos, facturas, asign_po, asign_fac, enlaces, cfg,
                              moneda_base="EUR")
    reporte = cost_report(lineas, pos, facturas, asign_po, asign_fac)

    return ResultadoConciliacion(
        project_id=project_id, lineas=lineas, proveedores=proveedores, pos=pos,
        facturas=facturas, asignaciones=asignaciones, asign_po=asign_po,
        asign_fac=asign_fac, enlaces=enlaces, anomalias=anomalias, reporte=reporte, cfg=cfg)
