"""Motor de conciliación banco ⇄ contabilidad de proveedores — precision-first.

Cruza las SALIDAS de la cuenta del banco con los pagos registrados en el mayor de
proveedores/acreedores (40/41) usando el **nº de asiento** como clave EXACTA: al
salir ambos ficheros del mismo programa contable, un pago comparte asiento en las
dos fichas.

Por cada salida del banco:
  - CASADO           -> su asiento existe en proveedores (pago registrado).
  - SIN_REGISTRO     -> su asiento NO está y el concepto parece pago a proveedor
                        ('Pago factura', 'Su Fra.', contrapartida 40/41…). HALLAZGO.
  - FUERA_DE_ALCANCE -> su asiento NO está y el concepto es claramente no-proveedor
                        (impuestos, Seg. Social, comisiones, efectivo, traspasos…).

Librería PURA y determinista. Filosofía §2: solo se destaca como SIN_REGISTRO lo
que positivamente parece un pago a proveedor; lo demás se agrupa fuera de alcance.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from ..domain.banco import (
    CERO,
    EstadoConciliacion,
    ExtractoBanco,
    InformeConciliacion,
    LineaConciliacion,
    MovimientoBanco,
    PagoSinBanco,
    ResumenConciliacion,
)
from ..domain.models import LibroMayor, Movimiento, TipoMovimiento
from .detector import es_cuenta_proveedor_acreedor, es_cuenta_tecnica

# Ventana (días) para acotar el aviso inverso al periodo del fichero del banco.
VENTANA_DIAS = 5

# Conceptos claramente NO-proveedor -> fuera de alcance, con su categoría.
_CATEGORIAS_NO_PROVEEDOR: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Impuestos / Hacienda",
     ("mod ", "modelo ", "impuesto", " iva", "irpf", "hacienda", "aeat",
      "tributo", "autoliquidac", "liquidacion iva", "tasa ", "censo")),
    ("Seguridad Social / Nóminas",
     ("seguridad social", "tgss", "seg social", "nomina", "nómina", "salario",
      "cotizacion", "tc1", "tc2", "finiquito")),
    ("Comisiones / Gastos banco",
     ("comision", "comisiones", "gastos banc", "mantenimiento", "intereses",
      "interes ", "tarjeta", "correo banc")),
    ("Efectivo / Traspasos",
     ("efectivo", "retirada", "cajero", "traspaso", "transferencia interna",
      "transf interna", "ingreso caja")),
    ("Préstamos / Leasing",
     ("prestamo", "préstamo", "leasing", "renting", "amortizacion prestamo",
      "cuota prestamo", "hipoteca")),
)

# Señales POSITIVAS de que una salida es un pago a proveedor (-> SIN_REGISTRO).
_SENALES_PROVEEDOR = ("pago factura", "pago fra", "su fra", "su factura",
                      "factura", " fra ", "fra.", "recibo", "proveedor", "acreedor")


class MotorConciliacionBanco:
    """Cruza banco y proveedores por asiento. Stateless entre llamadas."""

    def __init__(self, ventana_dias: int = VENTANA_DIAS) -> None:
        self.ventana_dias = ventana_dias

    # ----------------------------------------------------------------- público
    def conciliar(self, libro: LibroMayor, extracto: ExtractoBanco) -> InformeConciliacion:
        # Índice de asientos registrados en proveedores + apunte representativo.
        asientos_prov, pagos_por_asiento = self._indice_proveedores(libro)

        salidas = tuple(sorted(
            (m for m in extracto.movimientos if m.es_salida),
            key=lambda m: m.orden,
        ))
        lineas = tuple(
            self._conciliar_salida(s, asientos_prov, pagos_por_asiento)
            for s in salidas
        )
        pagos_sin_banco = self._pagos_sin_banco(libro, salidas)

        resumen = self._resumir(lineas, pagos_sin_banco, salidas)
        advertencias = list(extracto.advertencias_parseo)
        if not any(s.asiento for s in salidas):
            advertencias.append(
                "Las salidas del banco no traen nº de asiento: no se puede cruzar "
                "con la contabilidad de forma fiable."
            )
        return InformeConciliacion(
            lineas=lineas, pagos_sin_banco=pagos_sin_banco,
            resumen=resumen, advertencias=tuple(advertencias),
        )

    # ------------------------------------------------------------- índice
    @staticmethod
    def _indice_proveedores(
        libro: LibroMayor,
    ) -> tuple[set[str], dict[str, list[Movimiento]]]:
        """(asientos con apunte en 40/41, {asiento: [pagos de proveedor]})."""
        asientos: set[str] = set()
        pagos: dict[str, list[Movimiento]] = defaultdict(list)
        for m in libro.movimientos:
            if not es_cuenta_proveedor_acreedor(m.codigo_cuenta):
                continue
            if es_cuenta_tecnica(m.codigo_cuenta):
                continue
            if not m.asiento:
                continue
            asientos.add(m.asiento)
            if m.tipo == TipoMovimiento.PAGO and m.debe > CERO:
                pagos[m.asiento].append(m)
        return asientos, pagos

    # --------------------------------------------------------- por salida
    def _conciliar_salida(self, banco: MovimientoBanco, asientos_prov: set[str],
                          pagos_por_asiento: dict[str, list[Movimiento]]) -> LineaConciliacion:
        if banco.asiento and banco.asiento in asientos_prov:
            pago = self._mejor_pago(pagos_por_asiento.get(banco.asiento, []),
                                    banco.importe_abs)
            info = {}
            if pago is not None:
                info = dict(pago_codigo_cuenta=pago.codigo_cuenta,
                            pago_nombre_cuenta=pago.nombre_cuenta,
                            pago_importe=pago.debe)
            return LineaConciliacion(
                banco=banco, estado=EstadoConciliacion.CASADO,
                motivo=(f"El asiento {banco.asiento} está registrado en "
                        f"proveedores/acreedores: pago contabilizado."),
                senales=("asiento",), **info,
            )

        # El asiento no está en proveedores: ¿parece pago a proveedor o no?
        categoria = self._categoria_no_proveedor(banco)
        if categoria is not None:
            return LineaConciliacion(
                banco=banco, estado=EstadoConciliacion.FUERA_DE_ALCANCE,
                categoria=categoria,
                motivo=(f"Salida de {banco.importe_abs} € no registrada en "
                        f"proveedores; el concepto corresponde a «{categoria}», "
                        f"fuera del alcance de pagos a proveedores."),
            )
        return LineaConciliacion(
            banco=banco, estado=EstadoConciliacion.SIN_REGISTRO,
            motivo=(f"Salida de {banco.importe_abs} € el {self._fmt(banco.fecha)} "
                    f"(asiento {banco.asiento or '—'}) que parece un pago a "
                    f"proveedor pero NO está registrada en la contabilidad de "
                    f"proveedores/acreedores. Revisar con prioridad."),
            senales=("concepto pago",),
        )

    @staticmethod
    def _mejor_pago(pagos: list[Movimiento], importe: Decimal) -> Movimiento | None:
        """El pago del asiento cuyo importe más se acerca al de la salida."""
        if not pagos:
            return None
        return min(pagos, key=lambda p: (abs(p.debe - importe), p.orden))

    def _categoria_no_proveedor(self, banco: MovimientoBanco) -> str | None:
        """Categoría 'fuera de alcance' del concepto, o None si parece pago a
        proveedor (y por tanto es un hallazgo SIN_REGISTRO)."""
        texto = f"{banco.concepto} {banco.contrapartida or ''}".lower()
        for categoria, tokens in _CATEGORIAS_NO_PROVEEDOR:
            if any(t in texto for t in tokens):
                return categoria
        # Contrapartida que es una cuenta de proveedor/acreedor -> es pago a prov.
        cp = (banco.contrapartida or "").strip()
        if cp[:2] in ("40", "41"):
            return None
        # Señal positiva de proveedor en el concepto -> hallazgo.
        if any(s in texto for s in _SENALES_PROVEEDOR):
            return None
        # Ni señal de proveedor ni categoría conocida: para reducir ruido, se
        # agrupa como 'Otros (sin identificar)' en vez de afirmar un hallazgo.
        return "Otros (sin identificar)"

    # --------------------------------------------------------- inverso
    def _pagos_sin_banco(self, libro: LibroMayor,
                         salidas: tuple[MovimientoBanco, ...]) -> tuple[PagoSinBanco, ...]:
        fechas = [s.fecha for s in salidas if s.fecha is not None]
        if not fechas:
            return ()
        margen = timedelta(days=self.ventana_dias)
        desde, hasta = min(fechas) - margen, max(fechas) + margen
        asientos_banco = {s.asiento for s in salidas if s.asiento}

        out: list[PagoSinBanco] = []
        for m in libro.movimientos:
            if m.tipo != TipoMovimiento.PAGO or m.debe <= CERO:
                continue
            if not es_cuenta_proveedor_acreedor(m.codigo_cuenta) or es_cuenta_tecnica(m.codigo_cuenta):
                continue
            if m.fecha is None or not (desde <= m.fecha <= hasta):
                continue
            if m.asiento and m.asiento in asientos_banco:
                continue
            out.append(PagoSinBanco(
                codigo_cuenta=m.codigo_cuenta, nombre_cuenta=m.nombre_cuenta,
                asiento=m.asiento, fecha=m.fecha, importe=m.debe,
                referencia=self._ref_pago(m), comentario=m.comentario,
            ))
        return tuple(out)

    # ------------------------------------------------------------- utilidades
    @staticmethod
    def _ref_pago(m: Movimiento) -> str | None:
        r = m.referencias
        return r.su_factura or r.factura or r.documento_conta

    @staticmethod
    def _fmt(f: date | None) -> str:
        return f.isoformat() if f else "sin fecha"

    @staticmethod
    def _resumir(lineas, pagos_sin_banco, salidas) -> ResumenConciliacion:
        def imp(estado):
            return _suma(l.banco.importe_abs for l in lineas if l.estado == estado)
        def cnt(estado):
            return sum(1 for l in lineas if l.estado == estado)
        E = EstadoConciliacion
        fechas = [s.fecha for s in salidas if s.fecha is not None]
        return ResumenConciliacion(
            n_salidas_banco=len(salidas),
            n_casados=cnt(E.CASADO), importe_casado=imp(E.CASADO),
            n_sin_registro=cnt(E.SIN_REGISTRO), importe_sin_registro=imp(E.SIN_REGISTRO),
            n_fuera_alcance=cnt(E.FUERA_DE_ALCANCE), importe_fuera_alcance=imp(E.FUERA_DE_ALCANCE),
            n_revisar=cnt(E.REVISAR), importe_revisar=imp(E.REVISAR),
            n_pagos_sin_banco=len(pagos_sin_banco),
            importe_pagos_sin_banco=_suma(p.importe for p in pagos_sin_banco),
            fecha_desde=min(fechas) if fechas else None,
            fecha_hasta=max(fechas) if fechas else None,
        )


def _suma(valores) -> Decimal:
    total = CERO
    for v in valores:
        total += v
    return total.quantize(Decimal("0.01"))
