"""Motor del análisis inverso: FACTURAS SIN PAGO.

Espejo del motor de pagos-sin-factura, con la misma filosofía precision-first y
las mismas salvaguardas (exclusiones, fiabilidad por saldo, fuera de alcance).

Clasificación por cuenta (lo único que se afirma):
  - FACTURA_SIN_PAGO: la cuenta tiene facturas y CERO pagos (Σ Debe = 0,
    Σ Haber > 0). No hay nada que netear: todas sus facturas están sin pagar.
  - REVISAR: infrapagada (Σ Haber > Σ Debe) pero con algún pago: pago parcial o
    a cuenta. No se puede precisar qué factura concreta queda sin pagar (eso es
    v2); se muestra el importe pendiente.
  - CONCILIADA: Σ Haber ≤ Σ Debe (pagada o sobrepagada) o sin facturas.

Aviso de dominio: una factura sin pagar suele ser deuda viva normal con el
proveedor. La ANTIGÜEDAD (aging) es la que distingue lo rutinario (reciente) de
lo notable (vencido/antiguo). Por eso el detalle incluye la antigüedad de cada
factura, calculada respecto a la fecha de corte del libro (determinista).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date
from decimal import Decimal

from ..domain.models import (
    CERO,
    TOLERANCIA,
    AperturaCuenta,
    Clasificacion,
    Confianza,
    LibroMayor,
    Movimiento,
    Origen,
    TipoMovimiento,
    etiqueta_grupo,
    tramo_aging,
)
from ..domain.resultados import (
    FacturaPendiente,
    Informe,
    ResultadoCuenta,
    Resumen,
)
from .detector import es_cuenta_proveedor_acreedor, es_cuenta_tecnica


class MotorFacturasSinPago:
    """Detección de facturas sin pago por cuenta. Stateless entre llamadas."""

    def __init__(self, tolerancia: Decimal = TOLERANCIA) -> None:
        self.tolerancia = tolerancia

    def analizar(self, libro: LibroMayor) -> Informe:
        por_cuenta: dict[str, list[Movimiento]] = defaultdict(list)
        for mov in libro.movimientos:
            por_cuenta[mov.codigo_cuenta].append(mov)

        # Fecha de corte = última fecha del libro (determinista, para el aging).
        fechas = [m.fecha for m in libro.movimientos if m.fecha is not None]
        corte = max(fechas) if fechas else None

        resultados: list[ResultadoCuenta] = []
        for codigo in sorted(por_cuenta):
            movs = tuple(sorted(por_cuenta[codigo], key=lambda m: m.orden))
            apertura = libro.aperturas.get(codigo, AperturaCuenta())
            resultados.append(
                self._analizar_cuenta(codigo, movs, apertura, libro.origen, corte))

        return Informe(
            resultados=tuple(resultados),
            resumen=self._resumir(resultados),
            flags_globales=self._flags_globales(libro),
            advertencias_parseo=libro.advertencias_parseo,
            huella=self._huella(libro),
        )

    # ------------------------------------------------------------ por cuenta
    def _analizar_cuenta(self, codigo, movs, apertura, origen, corte) -> ResultadoCuenta:
        nombre = movs[0].nombre_cuenta if movs else ""
        suma_debe = _suma(m.debe for m in movs)
        suma_haber = _suma(m.haber for m in movs)
        saldo_reportado = self._saldo_reportado(movs)
        saldo_reconstruido = (apertura.saldo_apertura + suma_debe - suma_haber
                              ).quantize(Decimal("0.01"))
        n_fact = sum(1 for m in movs if m.tipo == TipoMovimiento.FACTURA)
        n_pago = sum(1 for m in movs if m.tipo == TipoMovimiento.PAGO)
        n_abono = sum(1 for m in movs if m.tipo == TipoMovimiento.ABONO)

        flags: list[str] = []
        if origen == Origen.PDF:
            flags.append("ORIGEN_PDF")

        def res(clasif, conf, motivo, pendiente=CERO, facturas=(),
                subcategoria=None, subcategoria_motivo=""):
            return ResultadoCuenta(
                codigo_cuenta=codigo, nombre_cuenta=nombre, clasificacion=clasif,
                confianza=conf, motivo=motivo, suma_debe=suma_debe,
                suma_haber=suma_haber, saldo_reconstruido=saldo_reconstruido,
                saldo_reportado=saldo_reportado, num_facturas=n_fact,
                num_pagos=n_pago, num_abonos=n_abono, flags=tuple(flags),
                movimientos=movs, importe_pendiente_pago=pendiente, facturas=facturas,
                subcategoria=subcategoria, subcategoria_motivo=subcategoria_motivo,
            )

        # Salvaguardas (idénticas al análisis directo).
        if es_cuenta_tecnica(codigo):
            return res(Clasificacion.EXCLUIDA, Confianza.NA,
                       "Cuenta técnica/puente (4009/4109): se excluye del análisis.")
        if not es_cuenta_proveedor_acreedor(codigo):
            return res(Clasificacion.FUERA_DE_ALCANCE, Confianza.NA,
                       f"Fuera de alcance: {etiqueta_grupo(codigo)}, no proveedor/acreedor.")
        if saldo_reportado is not None and abs(saldo_reconstruido - saldo_reportado) > self.tolerancia:
            return res(Clasificacion.NO_FIABLE, Confianza.NA,
                       f"El saldo reconstruido ({saldo_reconstruido} €) no cuadra con "
                       f"el del fichero ({saldo_reportado} €); no se concluye.")

        # Sin facturas -> nada que comprobar en este análisis.
        if suma_haber <= self.tolerancia:
            return res(Clasificacion.CONCILIADA, Confianza.NA,
                       "Sin facturas registradas en la cuenta: nada que comprobar.")

        # FACTURA_SIN_PAGO: hay facturas y CERO pagos.
        if suma_debe <= self.tolerancia:
            facturas = self._detalle_facturas(movs, corte)
            return res(
                Clasificacion.FACTURA_SIN_PAGO, Confianza.ALTA,
                f"La cuenta tiene {n_fact} factura(s) por {suma_haber} € y CERO "
                f"pagos. Ninguna factura de esta cuenta ha sido pagada. Revisar "
                f"(puede ser deuda viva normal o pago pendiente).",
                pendiente=suma_haber, facturas=facturas)

        # REVISAR: infrapagada (queda saldo acreedor) pero con algún pago.
        if suma_haber > suma_debe + self.tolerancia:
            pendiente = (suma_haber - suma_debe).quantize(Decimal("0.01"))
            facturas = self._detalle_facturas(movs, corte)
            sub, sub_motivo = self._subclasificar_infrapago(
                facturas, n_abono, pendiente)
            return res(
                Clasificacion.REVISAR, Confianza.NA,
                f"Cuenta infrapagada: Σ Haber ({suma_haber} €) supera a Σ Debe "
                f"({suma_debe} €) en {pendiente} €. Pago parcial/a cuenta; no se "
                f"puede precisar qué factura concreta queda sin pagar (v2).",
                pendiente=pendiente, facturas=facturas,
                subcategoria=sub, subcategoria_motivo=sub_motivo)

        # CONCILIADA: pagada o sobrepagada.
        return res(Clasificacion.CONCILIADA, Confianza.NA,
                   f"Σ Haber ({suma_haber} €) ≤ Σ Debe ({suma_debe} €): las facturas "
                   f"están pagadas. Sin alerta.")

    def _detalle_facturas(self, movs, corte: date | None) -> tuple[FacturaPendiente, ...]:
        out = []
        for m in movs:
            if m.tipo not in (TipoMovimiento.FACTURA, TipoMovimiento.ABONO):
                continue
            ref_fecha = m.vencimiento or m.fecha
            dias = (corte - ref_fecha).days if (corte and ref_fecha) else None
            vencida = bool(m.vencimiento and corte and corte > m.vencimiento)
            out.append(FacturaPendiente(
                fecha=m.fecha, vencimiento=m.vencimiento, importe=m.haber,
                referencia=m.referencias.su_factura or m.referencias.factura,
                nif=m.referencias.nif, antiguedad_dias=dias, vencida=vencida,
                tramo=tramo_aging(dias), comentario=m.comentario,
            ))
        return tuple(out)

    def _subclasificar_infrapago(self, facturas, n_abono, pendiente):
        """Razón por la que una cuenta infrapagada queda en REVISAR (determinista).

        Ordenada de menor a mayor relevancia. Usa la antigüedad de las facturas.
        """
        # 1) Hay abonos -> el pendiente puede ser artefacto de la rectificativa.
        if n_abono > 0:
            return ("DISTORSION_POR_ABONO",
                    f"La cuenta tiene {n_abono} abono(s); el pendiente de "
                    f"{pendiente} € puede deberse al neteo con la rectificativa.")

        recientes = [f for f in facturas
                     if f.antiguedad_dias is not None and f.antiguedad_dias <= 30]
        suma_recientes = sum((f.importe for f in recientes), Decimal("0.00"))
        hay_antiguas = any(f.antiguedad_dias is not None and f.antiguedad_dias > 90
                           for f in facturas)

        # 2) El pendiente lo explican facturas recientes -> desfase de corte.
        if recientes and pendiente <= suma_recientes + self.tolerancia:
            return ("DESFASE_DE_CORTE",
                    f"El pendiente ({pendiente} €) encaja con facturas recientes "
                    f"(≤30 días del corte): probablemente pago pendiente normal.")

        # 3) Quedan facturas antiguas sin pagar -> deuda antigua (más relevante).
        if hay_antiguas:
            return ("DEUDA_ANTIGUA",
                    f"Hay facturas de más de 90 días y la cuenta sigue infrapagada "
                    f"({pendiente} € pendientes) pese a existir pagos. Revisar.")

        # 4) Resto: pago parcial / a cuenta.
        return ("PAGO_PARCIAL",
                f"Pago parcial o a cuenta: queda {pendiente} € por pagar.")

    # ------------------------------------------------------------- utilidades
    @staticmethod
    def _saldo_reportado(movs) -> Decimal | None:
        for m in reversed(movs):
            if m.saldo_reportado is not None:
                return m.saldo_reportado
        return None

    @staticmethod
    def _resumir(resultados) -> Resumen:
        fsp = [r for r in resultados if r.clasificacion == Clasificacion.FACTURA_SIN_PAGO]
        rev = [r for r in resultados if r.clasificacion == Clasificacion.REVISAR]
        return Resumen(
            n_facturas_sin_pago=len(fsp),
            importe_facturas_sin_pago=_suma(r.importe_pendiente_pago for r in fsp),
            n_revisar=len(rev),
            importe_revisar=_suma(r.importe_pendiente_pago for r in rev),
            importe_pendiente_total=_suma(
                r.importe_pendiente_pago for r in (fsp + rev)),
            n_conciliadas=sum(1 for r in resultados if r.clasificacion == Clasificacion.CONCILIADA),
            n_no_fiables=sum(1 for r in resultados if r.clasificacion == Clasificacion.NO_FIABLE),
            n_excluidas=sum(1 for r in resultados if r.clasificacion == Clasificacion.EXCLUIDA),
            n_fuera_alcance=sum(1 for r in resultados if r.clasificacion == Clasificacion.FUERA_DE_ALCANCE),
            n_en_alcance=sum(1 for r in resultados if r.clasificacion not in
                             (Clasificacion.FUERA_DE_ALCANCE, Clasificacion.EXCLUIDA)),
            n_cuentas=len(resultados),
        )

    @staticmethod
    def _flags_globales(libro: LibroMayor) -> tuple[str, ...]:
        flags = []
        if libro.origen == Origen.PDF:
            flags.append("INFORME_SOBRE_PDF_DATOS_DEGRADADOS")
        if not libro.aperturas or all(a.ausente for a in libro.aperturas.values()):
            flags.append("SIN_SALDO_APERTURA")
        return tuple(flags)

    @staticmethod
    def _huella(libro: LibroMayor) -> str:
        h = hashlib.sha256()
        for m in sorted(libro.movimientos, key=lambda x: (x.codigo_cuenta, x.orden)):
            h.update(f"{m.codigo_cuenta}|{m.orden}|{m.debe}|{m.haber}|FSP".encode())
        return h.hexdigest()[:16]


def _suma(valores) -> Decimal:
    total = CERO
    for v in valores:
        total += v
    return total.quantize(Decimal("0.01"))
