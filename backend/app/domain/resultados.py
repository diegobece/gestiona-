"""Estructuras de salida del motor de detección.

Cada veredicto viaja SIEMPRE con su evidencia y su motivo. Nunca una etiqueta
suelta: el principio §2 (cero falsos positivos) exige que el humano pueda
auditar cada decisión.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .models import Clasificacion, Confianza, Movimiento


@dataclass(frozen=True)
class FacturaCandidata:
    """Factura más probable para un pago concreto (asistencia al revisor).

    NO es una afirmación del motor: es una sugerencia con su evidencia y un nivel
    de confianza, para que el humano confirme. Se calcula por importe exacto +
    proximidad de fecha dentro de las cuentas del MISMO proveedor.
    """

    pago_orden: int  # enlaza con el Movimiento de pago (su `orden`)
    pago_fecha: date | None
    pago_importe: Decimal
    factura_cuenta: str
    factura_nombre: str
    factura_fecha: date | None
    factura_importe: Decimal
    factura_ref: str | None
    dias_desfase: int | None  # días entre factura y pago (>=0 = factura antes)
    confianza: str            # "ALTA" | "MEDIA" | "BAJA"
    motivo: str
    fuente: str = "cuenta del proveedor"  # o "cuenta genérica (acreedores/varios)"
    senales: tuple[str, ...] = ()  # señales que casaron (importe, NIF, nombre, fecha…)


@dataclass(frozen=True)
class FacturaPendiente:
    """Una factura sin pago, con su antigüedad (análisis inverso)."""

    fecha: date | None
    vencimiento: date | None
    importe: Decimal
    referencia: str | None
    nif: str | None
    antiguedad_dias: int | None  # respecto a la fecha de corte del libro
    vencida: bool                # True si hay vencimiento y ya pasó el corte
    tramo: str                   # "0–30 días", "31–60 días", …
    comentario: str = ""


@dataclass(frozen=True)
class ResultadoCuenta:
    """Veredicto auditable de una cuenta de proveedor/acreedor."""

    codigo_cuenta: str
    nombre_cuenta: str
    clasificacion: Clasificacion
    confianza: Confianza
    motivo: str  # explicación legible de por qué se clasificó así

    suma_debe: Decimal
    suma_haber: Decimal
    saldo_reconstruido: Decimal
    saldo_reportado: Decimal | None

    num_facturas: int
    num_pagos: int
    num_abonos: int

    # Solo para REVISAR: por qué la cuenta sobrepagada cae aquí (sub-casilla de
    # triage). Es un valor de `SubcategoriaRevisar`. None para el resto.
    subcategoria: str | None = None
    subcategoria_motivo: str = ""

    # Solo para FACTURA_EN_OTRA_CUENTA: factura candidata por cada pago (asistencia).
    candidatos: tuple[FacturaCandidata, ...] = ()

    # Análisis inverso (facturas sin pago): importe pendiente de pago y detalle.
    importe_pendiente_pago: Decimal = Decimal("0.00")
    facturas: tuple[FacturaPendiente, ...] = ()

    # Banderas de contexto que matizan la confianza (no la afirmación):
    #   "SALDO_APERTURA_AUSENTE", "PAGO_EN_PRIMER_PERIODO", "ORIGEN_PDF", ...
    flags: tuple[str, ...] = ()

    # Evidencia completa: todos los movimientos de la cuenta, en orden.
    movimientos: tuple[Movimiento, ...] = ()

    @property
    def importe_sospechoso(self) -> Decimal:
        """€ no respaldados = exceso de pagos sobre facturas (>=0)."""
        diff = self.suma_debe - self.suma_haber
        return diff if diff > 0 else Decimal("0.00")


@dataclass(frozen=True)
class Resumen:
    """Agregados para la cabecera del informe."""

    n_sin_factura: int = 0
    importe_sin_factura: Decimal = Decimal("0.00")
    n_revisar: int = 0
    importe_revisar: Decimal = Decimal("0.00")
    # Análisis inverso (facturas sin pago):
    n_facturas_sin_pago: int = 0
    importe_facturas_sin_pago: Decimal = Decimal("0.00")
    importe_pendiente_total: Decimal = Decimal("0.00")
    n_conciliadas: int = 0
    n_no_fiables: int = 0
    n_excluidas: int = 0          # técnicas/puente (4009/4109)
    n_fuera_alcance: int = 0      # no proveedor/acreedor (clientes, bancos, IVA…)
    n_en_alcance: int = 0         # cuentas de proveedor/acreedor analizadas
    n_cuentas: int = 0


@dataclass(frozen=True)
class Informe:
    """Salida completa del análisis de un Libro Mayor."""

    resultados: tuple[ResultadoCuenta, ...]
    resumen: Resumen
    flags_globales: tuple[str, ...] = ()
    advertencias_parseo: tuple[str, ...] = ()
    huella: str = ""  # hash determinista de la entrada, para trazabilidad
