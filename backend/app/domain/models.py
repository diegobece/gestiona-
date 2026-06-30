"""Modelo canónico del dominio.

Toda la ingesta (Excel / PDF) produce `Movimiento`. El motor de detección
solo conoce esta estructura — nunca toca pandas, ficheros ni la UI.

Diseño precision-first: los importes son `Decimal` (no float) para que el
cálculo sea exacto y determinista. Mismo fichero -> mismo resultado, siempre.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


# Tolerancia de cuadre en euros. Por debajo de esto consideramos que dos
# importes "cuadran" (redondeos de céntimo en origen).
TOLERANCIA = Decimal("0.02")

CERO = Decimal("0.00")


class TipoMovimiento(str, Enum):
    """Clasificación de cada apunte contable.

    - FACTURA: factura recibida (apunte en Haber, comentario `Su Fra.: ...`).
    - PAGO:    pago al proveedor (apunte en Debe, comentario `Pago factura`).
    - ABONO:   factura rectificativa / abono (Haber negativo). Reduce la deuda.
    - OTRO:    apunte que no encaja en lo anterior; el motor lo arrastra al
               balance pero no lo trata como factura ni como pago.
    """

    FACTURA = "FACTURA"
    PAGO = "PAGO"
    ABONO = "ABONO"
    OTRO = "OTRO"


class Clasificacion(str, Enum):
    """Veredicto por cuenta. Es lo *único* que el sistema afirma."""

    SIN_FACTURA_ALTA_CONFIANZA = "SIN_FACTURA_ALTA_CONFIANZA"
    FACTURA_SIN_PAGO = "FACTURA_SIN_PAGO"  # análisis inverso: facturas sin ningún pago
    REVISAR = "REVISAR"
    CONCILIADA = "CONCILIADA"
    NO_FIABLE = "NO_FIABLE"
    EXCLUIDA = "EXCLUIDA"            # cuenta técnica/puente dentro del dominio (4009/4109)
    FUERA_DE_ALCANCE = "FUERA_DE_ALCANCE"  # no es proveedor/acreedor (cliente, banco, IVA…)


# Etiquetas de grupo del PGC, para explicar qué hay fuera de alcance.
_GRUPOS_PGC: dict[str, str] = {
    "43": "Clientes", "44": "Deudores varios", "46": "Personal",
    "47": "Administraciones públicas (IVA/Hacienda)",
    "50": "Empréstitos", "52": "Deudas a corto plazo",
    "55": "Otras cuentas financieras", "57": "Tesorería (caja/bancos)",
    "60": "Compras", "62": "Servicios exteriores", "63": "Tributos",
    "64": "Gastos de personal", "70": "Ventas", "75": "Otros ingresos",
}
_GRUPOS_DIGITO: dict[str, str] = {
    "4": "Acreedores/deudores", "5": "Cuentas financieras",
    "6": "Compras y gastos", "7": "Ingresos",
}


# Tramos de antigüedad (aging) para facturas pendientes de pago.
_AGING = [(30, "0–30 días"), (60, "31–60 días"), (90, "61–90 días")]
AGING_ORDEN = ["0–30 días", "31–60 días", "61–90 días", "+90 días"]


# Razones por las que una cuenta infrapagada (facturas) queda en REVISAR.
# Ordenadas de MENOR a MAYOR relevancia de impago.
SUBCATEGORIA_FSP_INFO: dict[str, dict[str, str]] = {
    "DESFASE_DE_CORTE": {
        "etiqueta": "Desfase de corte",
        "accion": "Las facturas pendientes son recientes (cerca del corte): probable pago pendiente normal.",
    },
    "DISTORSION_POR_ABONO": {
        "etiqueta": "Distorsión por abono",
        "accion": "Hay abonos/rectificativas; el pendiente puede ser un artefacto. Netear.",
    },
    "PAGO_PARCIAL": {
        "etiqueta": "Pago parcial",
        "accion": "Pago parcial o a cuenta; queda saldo pendiente de pagar.",
    },
    "DEUDA_ANTIGUA": {
        "etiqueta": "Deuda antigua",
        "accion": "Quedan facturas antiguas (+90 días) sin pagar pese a existir pagos: revisar con prioridad.",
    },
}


def incluir_en_informe_facturas(clasificacion, explicito: bool | None) -> bool:
    """¿Entra una cuenta en el PDF de facturas sin pago?

    FACTURA_SIN_PAGO: por defecto SÍ (se puede ocultar). REVISAR: por defecto NO
    (se añade explícitamente al revisarla y confirmarla). El resto, nunca.
    """
    if clasificacion == Clasificacion.FACTURA_SIN_PAGO:
        return True if explicito is None else explicito
    if clasificacion == Clasificacion.REVISAR:
        return False if explicito is None else explicito
    return False


def tramo_aging(dias: int | None) -> str:
    if dias is None:
        return "sin fecha"
    for limite, etiqueta in _AGING:
        if dias <= limite:
            return etiqueta
    return "+90 días"


def etiqueta_grupo(codigo: str) -> str:
    """Etiqueta legible del grupo contable de una cuenta fuera de alcance."""
    if len(codigo) >= 2 and codigo[:2] in _GRUPOS_PGC:
        return _GRUPOS_PGC[codigo[:2]]
    if codigo and codigo[0] in _GRUPOS_DIGITO:
        return _GRUPOS_DIGITO[codigo[0]]
    return "Otros"


class Confianza(str, Enum):
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    NA = "NA"  # no aplica (no se afirma nada sobre la cuenta)


class SubcategoriaRevisar(str, Enum):
    """Por qué una cuenta sobrepagada cae en REVISAR (no se afirma nada; es
    triage para que el humano sepa dónde mirar y se reduzcan falsos positivos).

    Ordenadas de MENOR a MAYOR sospecha de ser un pago realmente sin factura.
    """

    # El mismo proveedor aparece en otra cuenta que SÍ tiene facturas: la factura
    # probablemente está allí. Mínima sospecha de ser un pago sin factura.
    FACTURA_EN_OTRA_CUENTA = "FACTURA_EN_OTRA_CUENTA"
    # El crédito (Haber) no es una factura reconocida: una reversión de pago,
    # un apunte suelto... Hay que ver qué es. Puede esconder un pago sin factura.
    CREDITO_NO_IDENTIFICADO = "CREDITO_NO_IDENTIFICADO"
    # Hay abonos/rectificativas (Haber negativo) que empujan el exceso: el
    # "sobrepago" puede ser un artefacto contable. Netear contra el abono.
    DISTORSION_POR_ABONO = "DISTORSION_POR_ABONO"
    # La cuenta abre pagando y NUNCA estuvo a crédito: esos pagos liquidan
    # facturas de un ejercicio anterior no incluido. Mínima sospecha.
    ARRASTRE_EJERCICIO_ANTERIOR = "ARRASTRE_EJERCICIO_ANTERIOR"
    # Operativa normal (la cuenta sí estuvo a crédito) que termina en un débito
    # explicable por el último pago: la factura llegará tras el corte.
    DESFASE_DE_CORTE = "DESFASE_DE_CORTE"
    # Débito estructural que no se explica por apertura ni por el último pago.
    # El candidato más fuerte a pago sin factura (pero con facturas en la cuenta).
    SOBREPAGO_REVISAR = "SOBREPAGO_REVISAR"


# Etiquetas y descripciones legibles para la UI / exports.
SUBCATEGORIA_INFO: dict[str, dict[str, str]] = {
    "FACTURA_EN_OTRA_CUENTA": {
        "etiqueta": "Factura posiblemente en otra cuenta",
        "accion": "El proveedor aparece en otra cuenta con facturas. Comprobar allí antes de concluir.",
    },
    "CREDITO_NO_IDENTIFICADO": {
        "etiqueta": "Crédito no identificado",
        "accion": "El Haber no es una factura (p.ej. reversión de pago). Revisar qué es.",
    },
    "DISTORSION_POR_ABONO": {
        "etiqueta": "Distorsión por abono",
        "accion": "Hay rectificativas; netear el exceso contra el abono.",
    },
    "ARRASTRE_EJERCICIO_ANTERIOR": {
        "etiqueta": "Arrastre de ejercicio anterior",
        "accion": "Abre pagando sin haber debido nunca: comprobar el mayor del año anterior.",
    },
    "DESFASE_DE_CORTE": {
        "etiqueta": "Desfase de corte",
        "accion": "Pago final cuya factura aún no se ha registrado: revisar facturas posteriores al corte.",
    },
    "SOBREPAGO_REVISAR": {
        "etiqueta": "Sobrepago a revisar",
        "accion": "Exceso no explicado por apertura ni por el último pago. Revisar con prioridad.",
    },
}


class Origen(str, Enum):
    EXCEL = "EXCEL"
    PDF = "PDF"


@dataclass(frozen=True)
class Referencias:
    """Identificadores del documento, tal como vienen en el origen."""

    serie: str | None = None
    factura: str | None = None
    documento_conta: str | None = None
    su_factura: str | None = None
    contrapartida: str | None = None
    tipo_factura: str | None = None
    nif: str | None = None  # NIF/CIF del tercero, para identidad fiable

    def as_dict(self) -> dict[str, str | None]:
        return {
            "serie": self.serie,
            "factura": self.factura,
            "documento_conta": self.documento_conta,
            "su_factura": self.su_factura,
            "contrapartida": self.contrapartida,
            "tipo_factura": self.tipo_factura,
            "nif": self.nif,
        }


@dataclass(frozen=True)
class Movimiento:
    """Un apunte contable canónico.

    `debe` y `haber` son la fuente de verdad (tal como en el Libro Mayor).
    `importe_con_signo` = debe - haber es la contribución al saldo de la cuenta.
    Para un abono (haber negativo) el signo sale solo: debe=0, haber=-5.74 ->
    importe_con_signo = +5.74.
    """

    codigo_cuenta: str
    nombre_cuenta: str
    fecha: date | None
    asiento: str
    tipo: TipoMovimiento
    debe: Decimal
    haber: Decimal
    comentario: str
    referencias: Referencias
    orden: int  # posición original en el fichero; preserva el orden contable
    origen: Origen
    saldo_reportado: Decimal | None = None  # SaldoActual del origen, para validar
    vencimiento: date | None = None  # FechaVencimiento de la factura, si consta

    @property
    def importe_con_signo(self) -> Decimal:
        """Contribución al saldo de la cuenta (Debe - Haber)."""
        return self.debe - self.haber


@dataclass(frozen=True)
class AperturaCuenta:
    """Saldo de apertura (sumas anteriores) de una cuenta, si el fichero lo trae."""

    debe_anterior: Decimal = CERO
    haber_anterior: Decimal = CERO

    @property
    def saldo_apertura(self) -> Decimal:
        return self.debe_anterior - self.haber_anterior

    @property
    def ausente(self) -> bool:
        """True si el fichero no aporta saldo de apertura (ambos a 0)."""
        return self.debe_anterior == CERO and self.haber_anterior == CERO


@dataclass(frozen=True)
class LibroMayor:
    """Resultado de la ingesta: todos los movimientos + metadatos del parseo."""

    movimientos: tuple[Movimiento, ...]
    aperturas: dict[str, AperturaCuenta] = field(default_factory=dict)
    origen: Origen = Origen.EXCEL
    advertencias_parseo: tuple[str, ...] = ()
