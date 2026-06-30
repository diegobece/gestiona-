"""Modelo canónico de la Conciliación Presupuestaria (Presupuesto ⇄ PO ⇄ Factura).

Todo se normaliza a estas estructuras: el resto del módulo es agnóstico al formato
de origen (Excel/CSV/JSON). Importes en `Decimal` (exacto y determinista), igual
que el resto de la plataforma. Diseñado multi-proyecto desde el inicio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

CERO = Decimal("0.00")


class Nivel(str, Enum):
    TOPSHEET = "topsheet"
    ACCOUNT = "account"
    DETAIL = "detail"


class EstadoPO(str, Enum):
    OPEN = "open"
    PARTIAL = "partial"
    CLOSED = "closed"


class EstadoFactura(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    REVIEW = "review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class MetodoAsignacion(str, Enum):
    PO_REF = "po_ref"                 # nº de PO exacto
    ACCOUNT_CODE = "account_code"     # código de cuenta explícito
    VENDOR_AMOUNT = "vendor_amount"   # proveedor + importe + fecha (fuzzy)
    LLM = "llm_classification"        # sugerencia semántica (fase 3)
    MANUAL = "manual"                 # decidido por el humano


class EstadoAsignacion(str, Enum):
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Severidad(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class TipoEntidad(str, Enum):
    PO = "po"
    INVOICE = "invoice"


@dataclass(frozen=True)
class Proyecto:
    id: str
    nombre: str
    moneda_base: str = "EUR"
    periodo_inicio: date | None = None
    periodo_fin: date | None = None


@dataclass(frozen=True)
class LineaPresupuesto:
    id: str
    project_id: str
    code: str
    description: str
    budget_amount: Decimal           # presupuesto (se guarda neto y bruto)
    budget_amount_bruto: Decimal
    nivel: Nivel = Nivel.DETAIL
    parent_code: str | None = None
    department: str | None = None
    is_fringe: bool = False
    pgc_cuenta: str | None = None     # mapeo PGC (6xx gasto)


@dataclass(frozen=True)
class Proveedor:
    id: str
    nombre: str
    nombre_norm: str
    cif: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class LineaDoc:
    """Línea de detalle de una PO o factura (soporta split coding)."""
    descripcion: str
    importe: Decimal
    budget_line_id: str | None = None


@dataclass(frozen=True)
class OrdenCompra:
    id: str
    project_id: str
    po_number: str
    proveedor_id: str | None
    fecha: date | None
    net: Decimal
    tax: Decimal
    total: Decimal
    moneda: str = "EUR"
    estado: EstadoPO = EstadoPO.OPEN
    budget_line_id: str | None = None
    lineas: tuple[LineaDoc, ...] = ()
    source_file: str | None = None
    source_format: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Factura:
    id: str
    project_id: str
    invoice_number: str
    proveedor_id: str | None
    fecha: date | None
    net: Decimal
    tax: Decimal
    total: Decimal
    tax_rate: Decimal | None = None
    irpf: Decimal = CERO
    service_date: date | None = None
    moneda: str = "EUR"
    po_id: str | None = None
    budget_line_id: str | None = None
    estado: EstadoFactura = EstadoFactura.PENDING
    es_abono: bool = False            # nota de crédito (importes en negativo)
    lineas: tuple[LineaDoc, ...] = ()
    source_file: str | None = None
    source_format: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Asignacion:
    entity_type: TipoEntidad
    entity_id: str
    budget_line_id: str
    method: MetodoAsignacion
    confidence: Decimal              # 0..1
    estado: EstadoAsignacion = EstadoAsignacion.SUGGESTED
    motivo: str = ""


@dataclass(frozen=True)
class Anomalia:
    entity_type: TipoEntidad
    entity_id: str
    tipo: str
    severidad: Severidad
    detalle: str
    resuelta: bool = False
