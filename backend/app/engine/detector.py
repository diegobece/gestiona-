"""Motor de detección de pagos sin factura — v1, precision-first.

Librería PURA: sin I/O, sin pandas, sin UI. Recibe un `LibroMayor` canónico y
devuelve un `Informe`. Misma entrada -> misma salida, siempre (determinista).

Filosofía (§2): afirmamos solo lo que podemos probar. Una cuenta solo se marca
`SIN_FACTURA_ALTA_CONFIANZA` cuando NO existe ninguna factura en ella (Σ Haber
== 0) y sí hay pagos. Todo lo ambiguo -> `REVISAR`. Nunca emparejamos
pago<->factura por importe en v1 (genera decenas de falsos positivos: pagos
agrupados, parciales, netos contra abonos).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
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
    SubcategoriaRevisar,
    TipoMovimiento,
    etiqueta_grupo,
)
from ..domain.resultados import Informe, ResultadoCuenta, Resumen
from .candidatos import buscar_candidatas
from .conciliador import TOLERANCIA_DESCUADRE, conciliar_cuenta
from .proveedores import buscar_en_otras_cuentas, normalizar


def es_cuenta_proveedor_acreedor(codigo: str) -> bool:
    """True para proveedores (grupo 40) y acreedores (grupo 41) del PGC.

    Cubre 400/401/403… (proveedores) y 410/411… (acreedores). El resto del libro
    mayor (clientes 43, tesorería 57, Hacienda 47, ingresos 70, gastos 6x…) NO es
    proveedor/acreedor y queda FUERA DE ALCANCE de esta herramienta.
    """
    return codigo.startswith("40") or codigo.startswith("41")


def es_cuenta_tecnica(codigo: str) -> bool:
    """Cuentas puente/técnicas dentro del dominio de proveedores/acreedores.

    En particular `4009xxx`/`4109xxx` (FACTURAS PTES. RECIBIR): acumulan
    regularización sin facturas y sin excluirlas producirían un falso positivo
    enorme (en la muestra: Σ Debe ≈ 8.607 €, Σ Haber = 0).
    """
    return codigo.startswith("4009") or codigo.startswith("4109")


class MotorDeteccion:
    """Motor de detección por cuenta. Stateless entre llamadas."""

    def __init__(self, tolerancia: Decimal = TOLERANCIA) -> None:
        self.tolerancia = tolerancia

    # ----------------------------------------------------------------- público
    def analizar(self, libro: LibroMayor) -> Informe:
        movimientos_por_cuenta: dict[str, list[Movimiento]] = defaultdict(list)
        for mov in libro.movimientos:
            movimientos_por_cuenta[mov.codigo_cuenta].append(mov)

        # Índice de proveedores que SÍ tienen facturas, para detectar que la
        # factura de una cuenta sin facturas pueda estar en otra cuenta.
        indice = self._indice_cuentas_con_factura(movimientos_por_cuenta)

        resultados: list[ResultadoCuenta] = []
        # Orden determinista por código de cuenta.
        for codigo in sorted(movimientos_por_cuenta):
            movs = movimientos_por_cuenta[codigo]
            movs_ordenados = tuple(sorted(movs, key=lambda m: m.orden))
            apertura = libro.aperturas.get(codigo, AperturaCuenta())
            resultados.append(
                self._analizar_cuenta(codigo, movs_ordenados, apertura,
                                      libro.origen, indice)
            )

        resumen = self._resumir(resultados)
        flags_globales = self._flags_globales(libro)
        return Informe(
            resultados=tuple(resultados),
            resumen=resumen,
            flags_globales=flags_globales,
            advertencias_parseo=libro.advertencias_parseo,
            huella=self._huella(libro),
        )

    @staticmethod
    def _indice_cuentas_con_factura(
        movimientos_por_cuenta: dict[str, list[Movimiento]],
    ) -> dict[str, tuple[str, frozenset[str], tuple[Movimiento, ...]]]:
        """{codigo: (nombre, tokens, facturas)} para cuentas reales (400/410, no
        técnicas) con al menos una factura. Base para el cruce entre cuentas y
        para sugerir la factura candidata."""
        indice: dict[str, tuple[str, frozenset[str], tuple[Movimiento, ...]]] = {}
        for codigo, movs in movimientos_por_cuenta.items():
            if es_cuenta_tecnica(codigo) or not es_cuenta_proveedor_acreedor(codigo):
                continue
            facturas = tuple(m for m in movs if m.tipo == TipoMovimiento.FACTURA)
            if not facturas:
                continue
            nombre = movs[0].nombre_cuenta if movs else ""
            indice[codigo] = (nombre, normalizar(nombre), facturas)
        return indice

    # ------------------------------------------------------------ por cuenta
    def _analizar_cuenta(
        self,
        codigo: str,
        movs: tuple[Movimiento, ...],
        apertura: AperturaCuenta,
        origen: Origen,
        indice: dict[str, tuple[str, frozenset[str]]],
    ) -> ResultadoCuenta:
        nombre = movs[0].nombre_cuenta if movs else ""

        suma_debe = _suma(m.debe for m in movs)
        suma_haber = _suma(m.haber for m in movs)
        saldo_reportado = self._saldo_reportado(movs)
        saldo_reconstruido = (apertura.saldo_apertura + suma_debe - suma_haber).quantize(
            Decimal("0.01")
        )

        n_fact = sum(1 for m in movs if m.tipo == TipoMovimiento.FACTURA)
        n_pago = sum(1 for m in movs if m.tipo == TipoMovimiento.PAGO)
        n_abono = sum(1 for m in movs if m.tipo == TipoMovimiento.ABONO)

        flags: list[str] = []
        if origen == Origen.PDF:
            flags.append("ORIGEN_PDF")

        # --- Guardrail 1: cuenta técnica/puente -> EXCLUIDA -------------------
        if es_cuenta_tecnica(codigo):
            return self._resultado(
                codigo, nombre, Clasificacion.EXCLUIDA, Confianza.NA,
                "Cuenta técnica/puente (4009xxx FACTURAS PTES. RECIBIR): "
                "no es un proveedor real; se excluye del análisis.",
                suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                n_fact, n_pago, n_abono, tuple(flags), movs,
            )

        # --- Guardrail 2: no es proveedor/acreedor -> FUERA DE ALCANCE -------
        if not es_cuenta_proveedor_acreedor(codigo):
            return self._resultado(
                codigo, nombre, Clasificacion.FUERA_DE_ALCANCE, Confianza.NA,
                f"Fuera de alcance: no es cuenta de proveedor (40) ni acreedor "
                f"(41), sino {etiqueta_grupo(codigo)}. Esta herramienta solo "
                f"analiza pagos a proveedores/acreedores.",
                suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                n_fact, n_pago, n_abono, tuple(flags), movs,
            )

        # --- Guardrail 3: saldo reconstruido no cuadra -> NO_FIABLE ----------
        if saldo_reportado is not None:
            desvio = abs(saldo_reconstruido - saldo_reportado)
            if desvio > self.tolerancia:
                return self._resultado(
                    codigo, nombre, Clasificacion.NO_FIABLE, Confianza.NA,
                    f"El saldo reconstruido ({saldo_reconstruido} €) no cuadra con "
                    f"el SaldoActual del fichero ({saldo_reportado} €), desvío "
                    f"{desvio} €. El parseo no es fiable; no se concluye.",
                    suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                    n_fact, n_pago, n_abono, tuple(flags), movs,
                )

        # --- Aviso de saldo de apertura ausente ------------------------------
        # Si el fichero no trae apertura y hay pagos en el primer periodo, esos
        # pagos pueden liquidar facturas de un ejercicio NO incluido.
        pago_primer_periodo = self._hay_pago_en_primer_periodo(movs)
        if apertura.ausente and pago_primer_periodo:
            flags.append("SALDO_APERTURA_AUSENTE")
            flags.append("PAGO_EN_PRIMER_PERIODO")

        # --- Clasificación a nivel cuenta (lo ÚNICO que afirmamos) -----------
        return self._clasificar(
            codigo, nombre, suma_debe, suma_haber, saldo_reconstruido,
            saldo_reportado, n_fact, n_pago, n_abono, flags, movs, apertura, indice,
        )

    def _clasificar(
        self, codigo, nombre, suma_debe, suma_haber, saldo_reconstruido,
        saldo_reportado, n_fact, n_pago, n_abono, flags, movs, apertura, indice,
    ) -> ResultadoCuenta:
        # Sin pagos -> no hay nada que detectar.
        if suma_debe <= self.tolerancia:
            return self._resultado(
                codigo, nombre, Clasificacion.CONCILIADA, Confianza.NA,
                "Sin pagos registrados en la cuenta: nada que comprobar.",
                suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                n_fact, n_pago, n_abono, tuple(flags), movs,
            )

        # SIN_FACTURA_ALTA_CONFIANZA: hay pagos y CERO crédito (Σ Haber == 0).
        # No hay ninguna factura ni abono en la cuenta que netear.
        if suma_haber == CERO:
            # Guardrail anti-falso-positivo: ¿el mismo proveedor tiene facturas
            # en OTRA cuenta? Si es así, la factura podría estar allí -> NO se
            # afirma, va a REVISAR para verificación humana.
            otras = buscar_en_otras_cuentas(codigo, nombre, indice)
            if otras:
                lista = "; ".join(f"{c} {n}" for c, n in otras)
                # Factura candidata por pago (asistencia): facturas del proveedor
                # en esas otras cuentas, emparejadas por importe + fecha.
                # Pool de facturas a considerar: las de las cuentas del proveedor
                # (match de nombre) + las de cuentas genéricas (acreedores/proveedores
                # VARIOS, token vacío). El scorer por suma de señales decide la
                # confianza de cada candidata (nombre, NIF, comentario, fecha…).
                cuentas_match = {c for c, _ in otras}
                facturas_pool = [
                    f for c, d in indice.items()
                    if c != codigo and (c in cuentas_match or not d[1])
                    for f in d[2]
                ]
                pagos = [m for m in movs if m.tipo == TipoMovimiento.PAGO]
                candidatos = buscar_candidatas(pagos, facturas_pool, nombre)
                return self._resultado(
                    codigo, nombre, Clasificacion.REVISAR, Confianza.NA,
                    f"La cuenta no tiene facturas, pero el proveedor aparece en "
                    f"otra(s) cuenta(s) con facturas ({lista}). La factura podría "
                    f"estar allí: NO se afirma 'sin factura'; se manda a revisión.",
                    suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                    n_fact, n_pago, n_abono, tuple(flags), movs,
                    subcategoria=SubcategoriaRevisar.FACTURA_EN_OTRA_CUENTA.value,
                    subcategoria_motivo=(
                        f"Proveedor '{nombre}' presente también en: {lista}. "
                        f"Comprobar si el pago de esta cuenta corresponde a una "
                        f"factura registrada en esa(s) cuenta(s)."
                    ),
                    candidatos=candidatos,
                )
            confianza = Confianza.ALTA
            motivo = (
                f"La cuenta tiene {n_pago} pago(s) por {suma_debe} € y CERO "
                f"facturas (Σ Haber = 0). No existe factura en esta cuenta que "
                f"respalde el pago. Requiere verificación humana (§2)."
            )
            # Si falta el saldo de apertura y el pago cae en el primer periodo,
            # la factura podría ser de un ejercicio anterior: bajamos confianza.
            if "SALDO_APERTURA_AUSENTE" in flags:
                confianza = Confianza.MEDIA
                motivo += (
                    " AVISO: sin saldo de apertura y pago en el primer periodo; "
                    "la factura podría pertenecer a un ejercicio no incluido."
                )
            return self._resultado(
                codigo, nombre, Clasificacion.SIN_FACTURA_ALTA_CONFIANZA, confianza,
                motivo, suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                n_fact, n_pago, n_abono, tuple(flags), movs,
                pagos_sin_factura=tuple(
                    m for m in movs if m.tipo == TipoMovimiento.PAGO),
            )

        # Conciliación fina por subconjuntos (v2): un pago puede liquidar un GRUPO
        # de facturas (pago agrupado). Los pagos que no casan con ninguna factura ni
        # grupo de facturas son HUÉRFANOS = pago sin factura CONFIRMADO, aunque la
        # cuenta esté infrapagada en neto (el huérfano queda oculto entre las deudas).
        # Descuadres < tolerancia se dan por conciliados. Los guardarraíles (abonos,
        # abre-pagando) están dentro de conciliar_cuenta (devuelve None).
        huerfanos = conciliar_cuenta(movs)
        if huerfanos is not None:
            suma_huerf = _suma(m.debe for m in huerfanos)
            exceso_neto = (suma_debe - suma_haber).quantize(Decimal("0.01"))
            asientos = ", ".join(sorted({m.asiento for m in huerfanos if m.asiento}))

            # SOLO se afirma en cuentas SOBREPAGADAS (Σ Debe > Σ Haber) y con pagos
            # concretos sin casar. El exceso neto es dinero pagado por encima de TODO
            # lo facturado, que ninguna factura —ni un pago parcial— puede respaldar.
            if exceso_neto >= TOLERANCIA_DESCUADRE and suma_huerf >= TOLERANCIA_DESCUADRE:
                importe = min(suma_huerf, exceso_neto)
                return self._resultado(
                    codigo, nombre, Clasificacion.SIN_FACTURA_ALTA_CONFIANZA,
                    Confianza.ALTA,
                    f"Conciliación fina: los pagos casan con sus facturas (incluidos "
                    f"pagos agrupados y parciales), pero se pagaron {importe} € por "
                    f"encima de todo lo facturado (asiento(s) {asientos}). Ese exceso "
                    f"no lo respalda ninguna factura. Pago sin factura confirmado.",
                    suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                    n_fact, n_pago, n_abono,
                    tuple(list(flags) + ["PAGO_SIN_FACTURA_CONFIRMADO"]), movs,
                    importe_sin_factura_confirmado=importe,
                    pagos_sin_factura=tuple(huerfanos),
                )

            # Cuenta que DEBE dinero neto (Σ Haber ≥ Σ Debe): todos los pagos están
            # respaldados por facturas (no puede haber pago sin factura cuando se ha
            # facturado igual o más de lo pagado). Lo pendiente es una FACTURA sin
            # pagar y se trata en el análisis inverso "Facturas sin pago" — aquí NO
            # debe aparecer a revisar. CONCILIADA.
            if exceso_neto < CERO:
                motivo = (
                    f"La cuenta debe dinero neto (Σ Haber {suma_haber} € > Σ Debe "
                    f"{suma_debe} €): todos los pagos están respaldados por facturas. "
                    f"Sin pago sin factura; lo pendiente se ve en 'Facturas sin pago'.")
            else:
                # Sobrepago/cuadre explicado por abonos o arrastre, sin pagos sueltos.
                motivo = (
                    "Conciliación fina: todos los pagos casan con sus facturas "
                    "(agrupados/parciales); los descuadres bajo tolerancia y los "
                    "abonos/arrastre no dejan ningún pago sin factura. Sin alerta.")
            return self._resultado(
                codigo, nombre, Clasificacion.CONCILIADA, Confianza.NA, motivo,
                suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                n_fact, n_pago, n_abono, tuple(flags), movs,
            )

        # Cuenta NO apta para conciliación fina (tiene abonos o abre pagando).
        # REVISAR: sobrepagada (Σ Debe > Σ Haber) pero CON facturas. NO se afirma.
        if suma_debe > suma_haber + self.tolerancia:
            exceso = (suma_debe - suma_haber).quantize(Decimal("0.01"))
            sub, sub_motivo = self._subclasificar_revisar(
                movs, apertura, suma_debe, suma_haber, n_fact, n_abono, exceso
            )
            return self._resultado(
                codigo, nombre, Clasificacion.REVISAR, Confianza.NA,
                f"Cuenta sobrepagada: Σ Debe ({suma_debe} €) supera a Σ Haber "
                f"({suma_haber} €) en {exceso} €, pero existen {n_fact} factura(s). "
                f"Probable pago agrupado/parcial o arrastre. NO se afirma; revisar.",
                suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
                n_fact, n_pago, n_abono, tuple(flags), movs,
                subcategoria=sub.value, subcategoria_motivo=sub_motivo,
            )

        # CONCILIADA: Σ Debe <= Σ Haber (aún se debe o cuadra).
        return self._resultado(
            codigo, nombre, Clasificacion.CONCILIADA, Confianza.NA,
            f"Σ Debe ({suma_debe} €) ≤ Σ Haber ({suma_haber} €): los pagos no "
            f"exceden a las facturas. Sin alerta.",
            suma_debe, suma_haber, saldo_reconstruido, saldo_reportado,
            n_fact, n_pago, n_abono, tuple(flags), movs,
        )

    def _subclasificar_revisar(
        self,
        movs: tuple[Movimiento, ...],
        apertura: AperturaCuenta,
        suma_debe: Decimal,
        suma_haber: Decimal,
        n_fact: int,
        n_abono: int,
        exceso: Decimal,
    ) -> tuple[SubcategoriaRevisar, str]:
        """Asigna la sub-casilla de un REVISAR a partir de la trayectoria del
        saldo. Determinista y auditable. Prioridad de menor a mayor sospecha.

        Señales:
          - `estuvo_a_credito`: el saldo acumulado (desde la apertura) llegó a ser
            negativo (de verdad debimos dinero) en algún momento.
          - `ultimo_pago`: importe del último pago en orden contable.
        """
        # Reconstrucción cronológica del saldo (los movs ya vienen ordenados).
        saldo = apertura.saldo_apertura
        saldo_min = saldo
        ultimo_pago = CERO
        for m in movs:
            saldo += m.importe_con_signo
            if saldo < saldo_min:
                saldo_min = saldo
            if m.tipo == TipoMovimiento.PAGO:
                ultimo_pago = m.debe
        estuvo_a_credito = saldo_min < -self.tolerancia

        # 1) Hay abonos -> el exceso puede ser artefacto de la rectificativa.
        if n_abono > 0:
            return (
                SubcategoriaRevisar.DISTORSION_POR_ABONO,
                f"La cuenta tiene {n_abono} abono(s)/rectificativa(s). El exceso de "
                f"{exceso} € puede deberse al neteo con el abono, no a un pago "
                f"huérfano. Revisar el abono antes de concluir.",
            )

        # 2) Hay crédito (Σ Haber ≠ 0) pero NINGUNA factura reconocida.
        if n_fact == 0:
            return (
                SubcategoriaRevisar.CREDITO_NO_IDENTIFICADO,
                f"Hay {suma_haber} € en el Haber pero ningún apunte de factura "
                f"('Su Fra.: ...'): podría ser una reversión de pago u otro apunte. "
                f"Hay que ver qué respalda ese crédito.",
            )

        # 3) Nunca estuvo a crédito: abrió pagando -> arrastre de ejercicio.
        if not estuvo_a_credito:
            return (
                SubcategoriaRevisar.ARRASTRE_EJERCICIO_ANTERIOR,
                f"La cuenta nunca llegó a estar a crédito (saldo mínimo "
                f"{saldo_min.quantize(Decimal('0.01'))} €): abre pagando, así que "
                f"esos pagos liquidan facturas de un ejercicio anterior no incluido. "
                f"Comprobar el mayor del año previo.",
            )

        # 4) Estuvo a crédito y el último pago explica el débito final -> desfase.
        if exceso <= ultimo_pago + self.tolerancia:
            return (
                SubcategoriaRevisar.DESFASE_DE_CORTE,
                f"Operativa normal (la cuenta sí estuvo a crédito) que termina en un "
                f"débito de {exceso} € explicable por el último pago ({ultimo_pago} €): "
                f"la factura correspondiente llegará tras el corte. Revisar facturas "
                f"posteriores / pendientes de registrar.",
            )

        # 5) Resto: débito estructural -> mayor sospecha.
        return (
            SubcategoriaRevisar.SOBREPAGO_REVISAR,
            f"Débito de {exceso} € que no se explica por la apertura ni por el "
            f"último pago ({ultimo_pago} €): hay más pagos sin factura que respalde. "
            f"Es el candidato más fuerte a pago sin factura; revisar con prioridad.",
        )

    # ------------------------------------------------------------- utilidades
    @staticmethod
    def _saldo_reportado(movs: tuple[Movimiento, ...]) -> Decimal | None:
        """SaldoActual del último apunte (en orden contable), si existe."""
        for m in reversed(movs):
            if m.saldo_reportado is not None:
                return m.saldo_reportado
        return None

    @staticmethod
    def _hay_pago_en_primer_periodo(movs: tuple[Movimiento, ...]) -> bool:
        """¿Hay algún pago en el primer mes presente en la cuenta?"""
        fechas = [m.fecha for m in movs if m.fecha is not None]
        if not fechas:
            return False
        primer = min(fechas)
        clave_primer = (primer.year, primer.month)
        return any(
            m.tipo == TipoMovimiento.PAGO
            and m.fecha is not None
            and (m.fecha.year, m.fecha.month) == clave_primer
            for m in movs
        )

    @staticmethod
    def _resultado(
        codigo, nombre, clasificacion, confianza, motivo, suma_debe, suma_haber,
        saldo_reconstruido, saldo_reportado, n_fact, n_pago, n_abono, flags, movs,
        subcategoria=None, subcategoria_motivo="", candidatos=(),
        importe_sin_factura_confirmado=None, pagos_sin_factura=(),
    ) -> ResultadoCuenta:
        return ResultadoCuenta(
            codigo_cuenta=codigo,
            nombre_cuenta=nombre,
            clasificacion=clasificacion,
            confianza=confianza,
            motivo=motivo,
            suma_debe=suma_debe,
            suma_haber=suma_haber,
            saldo_reconstruido=saldo_reconstruido,
            saldo_reportado=saldo_reportado,
            num_facturas=n_fact,
            num_pagos=n_pago,
            num_abonos=n_abono,
            subcategoria=subcategoria,
            subcategoria_motivo=subcategoria_motivo,
            candidatos=candidatos,
            flags=flags,
            movimientos=movs,
            importe_sin_factura_confirmado=importe_sin_factura_confirmado,
            pagos_sin_factura=pagos_sin_factura,
        )

    @staticmethod
    def _resumir(resultados: list[ResultadoCuenta]) -> Resumen:
        sin = [r for r in resultados if r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA]
        rev = [r for r in resultados if r.clasificacion == Clasificacion.REVISAR]
        return Resumen(
            n_sin_factura=len(sin),
            importe_sin_factura=_suma(r.importe_sospechoso for r in sin),
            n_revisar=len(rev),
            importe_revisar=_suma(r.importe_sospechoso for r in rev),
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
        flags: list[str] = []
        if libro.origen == Origen.PDF:
            flags.append("INFORME_SOBRE_PDF_DATOS_DEGRADADOS")
        if all(a.ausente for a in libro.aperturas.values()) or not libro.aperturas:
            flags.append("SIN_SALDO_APERTURA")
        return tuple(flags)

    @staticmethod
    def _huella(libro: LibroMayor) -> str:
        """Hash determinista de la entrada, para trazar 'mismo fichero -> mismo id'."""
        h = hashlib.sha256()
        for m in sorted(libro.movimientos, key=lambda x: (x.codigo_cuenta, x.orden)):
            h.update(
                f"{m.codigo_cuenta}|{m.orden}|{m.debe}|{m.haber}|{m.tipo.value}".encode()
            )
        return h.hexdigest()[:16]


def _suma(valores) -> Decimal:
    total = CERO
    for v in valores:
        total += v
    return total.quantize(Decimal("0.01"))
