"""Modelo del extracto/ficha del banco y de la conciliación banco ⇄ contabilidad.

El fichero del banco que maneja el usuario es la **Ficha de Mayor de la cuenta de
tesorería** (p.ej. 5720002) exportada del mismo programa contable que el mayor de
proveedores. Como ambos salen del mismo sistema, cada pago comparte el **mismo nº
de asiento** en las dos fichas: esa es la clave de cruce EXACTA.

Convención de signo (cuenta de tesorería, activo): el **Haber** es una SALIDA
(pago) y el **Debe** una ENTRADA (cobro). `importe` va con signo: negativo =
salida. Diseño precision-first: `Decimal`, inmutable y determinista.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

CERO = Decimal("0.00")


@dataclass(frozen=True)
class MovimientoBanco:
    """Un apunte de la cuenta del banco, en modelo canónico.

    `importe` CON SIGNO: negativo = salida (pago); positivo = entrada (cobro).
    `asiento` es la clave de cruce con el mayor de proveedores.
    """

    fecha: date | None
    importe: Decimal
    concepto: str               # Comentario del apunte
    asiento: str | None         # nº de asiento (clave de cruce exacta)
    referencia: str | None      # nº documento/factura si consta
    contrapartida: str | None   # cuenta/tercero de contrapartida, si consta
    orden: int
    saldo: Decimal | None = None

    @property
    def es_salida(self) -> bool:
        return self.importe < CERO

    @property
    def importe_abs(self) -> Decimal:
        return abs(self.importe)


@dataclass(frozen=True)
class ExtractoBanco:
    movimientos: tuple[MovimientoBanco, ...]
    advertencias_parseo: tuple[str, ...] = ()


class EstadoConciliacion(str, Enum):
    """Veredicto de cada salida del banco frente a la contabilidad de proveedores."""

    # El asiento de la salida existe en el mayor de proveedores: pago registrado.
    CASADO = "CASADO"
    # El asiento NO está en proveedores y el concepto parece un pago a proveedor
    # ('Pago factura' o nombre de tercero): salió del banco sin registrarse. HALLAZGO.
    SIN_REGISTRO = "SIN_REGISTRO"
    # El asiento NO está en proveedores pero el concepto es claramente no-proveedor
    # (impuestos, comisiones, efectivo, traspasos, nóminas…): fuera de alcance.
    FUERA_DE_ALCANCE = "FUERA_DE_ALCANCE"
    # Reservado para cruces dudosos (p.ej. sin asiento en el fichero del banco).
    REVISAR = "REVISAR"


@dataclass(frozen=True)
class LineaConciliacion:
    """Una salida del banco con su veredicto y, si casó, el apunte de proveedor."""

    banco: MovimientoBanco
    estado: EstadoConciliacion
    motivo: str
    categoria: str | None = None  # para FUERA_DE_ALCANCE: 'Impuestos', 'Comisiones'…
    # Apunte de proveedor con el que casa (mismo asiento), si CASADO:
    pago_codigo_cuenta: str | None = None
    pago_nombre_cuenta: str | None = None
    pago_importe: Decimal | None = None
    senales: tuple[str, ...] = ()


@dataclass(frozen=True)
class PagoSinBanco:
    """Aviso inverso: un pago de proveedor cuyo asiento no aparece en el banco.

    Puede ser un pago por otra vía (caja, otro banco) o un impago. Aviso, no
    afirmación; se limita al rango de fechas del fichero del banco.
    """

    codigo_cuenta: str
    nombre_cuenta: str
    asiento: str | None
    fecha: date | None
    importe: Decimal
    referencia: str | None
    comentario: str


@dataclass(frozen=True)
class ResumenConciliacion:
    n_salidas_banco: int = 0
    n_casados: int = 0
    importe_casado: Decimal = CERO
    n_sin_registro: int = 0
    importe_sin_registro: Decimal = CERO
    n_fuera_alcance: int = 0
    importe_fuera_alcance: Decimal = CERO
    n_revisar: int = 0
    importe_revisar: Decimal = CERO
    n_pagos_sin_banco: int = 0
    importe_pagos_sin_banco: Decimal = CERO
    fecha_desde: date | None = None
    fecha_hasta: date | None = None


@dataclass(frozen=True)
class InformeConciliacion:
    lineas: tuple[LineaConciliacion, ...]
    pagos_sin_banco: tuple[PagoSinBanco, ...]
    resumen: ResumenConciliacion
    advertencias: tuple[str, ...] = ()
