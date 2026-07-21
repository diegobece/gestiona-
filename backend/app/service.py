"""Orquestación: ingesta -> motor -> informe. Capa fina sobre la librería pura.

Regla de origen (§4 / §8): si hay Excel, se usa el Excel (fuente autoritativa).
El PDF solo se usa como fallback. La inteligencia vive en el motor; esto solo
elige el parser y junta el resultado con los overrides persistidos.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .domain.banco import ExtractoBanco, InformeConciliacion
from .domain.models import Clasificacion, LibroMayor
from .domain.resultados import Informe
from .engine.conciliacion_banco import MotorConciliacionBanco
from .engine.detector import MotorDeteccion
from .engine.facturas import MotorFacturasSinPago
from .ingest.banco_parser import parsear_banco
from .ingest.excel_parser import parsear_excel
from .ingest.pdf_parser import parsear_pdf

_EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}
_PDF_EXT = {".pdf"}
_BANCO_EXT = {".xlsx", ".xlsm", ".xls", ".csv"}

_motor = MotorDeteccion()
_motor_facturas = MotorFacturasSinPago()
_motor_conciliacion = MotorConciliacionBanco()


def parsear(ruta: str | Path) -> LibroMayor:
    """Elige el parser por extensión. Excel siempre que sea posible."""
    ext = Path(ruta).suffix.lower()
    if ext in _EXCEL_EXT:
        return parsear_excel(ruta)
    if ext in _PDF_EXT:
        return parsear_pdf(ruta)
    raise ValueError(f"Formato no soportado: {ext}. Use Excel (.xlsx) o PDF.")


def analizar_fichero(ruta: str | Path) -> Informe:
    """Pipeline completo para un fichero en disco."""
    return _motor.analizar(parsear(ruta))


def analizar_libro(libro: LibroMayor) -> Informe:
    return _motor.analizar(libro)


def analizar_facturas_libro(libro: LibroMayor) -> Informe:
    """Análisis inverso: facturas sin pago."""
    return _motor_facturas.analizar(libro)


def parsear_extracto_banco(ruta: str | Path) -> ExtractoBanco:
    """Parsea un extracto bancario (Excel o CSV)."""
    ext = Path(ruta).suffix.lower()
    if ext not in _BANCO_EXT:
        raise ValueError(
            f"Formato de extracto no soportado: {ext}. Use Excel (.xlsx) o CSV."
        )
    return parsear_banco(ruta)


def conciliar_banco(libro: LibroMayor, extracto: ExtractoBanco) -> InformeConciliacion:
    """Cruza el extracto bancario con los pagos registrados en el Libro Mayor."""
    return _motor_conciliacion.conciliar(libro, extracto)


def _fecha_corte(libro: LibroMayor) -> date | None:
    """Fecha de corte del libro: el último apunte con fecha de todo el fichero."""
    fechas = [m.fecha for m in libro.movimientos if m.fecha is not None]
    return max(fechas) if fechas else None


def _fecha_corte_informe(informe: Informe) -> date | None:
    """Fecha de corte deducida del informe, cuando ya no se tiene el LibroMayor.

    La API guarda los informes, no el libro: este es el corte que se usa al
    revisar un análisis ya hecho.
    """
    fechas = [
        m.fecha
        for r in informe.resultados
        for m in r.movimientos
        if m.fecha is not None
    ]
    return max(fechas) if fechas else None


#: Clasificaciones que el motor considera ya resueltas y que el REPASO revisa.
#: EXCLUIDA / FUERA_DE_ALCANCE / NO_FIABLE no se repasan: no son decisiones de
#: fondo sobre la cuenta, sino que quedó fuera del alcance del análisis.
_DECIDIDAS = (
    Clasificacion.CONCILIADA,
    Clasificacion.SIN_FACTURA_ALTA_CONFIANZA,
    Clasificacion.FACTURA_SIN_PAGO,
)


def razonar_revisar(informe: Informe, libro: LibroMayor | None = None) -> dict:
    """Segunda opinión (Claude Opus 4.8) SOLO sobre las cuentas en REVISAR.

    Devuelve {codigo_cuenta: SugerenciaRazonador} para las cuentas que el motor
    dejó en REVISAR. El motor sigue siendo la fuente de verdad; esto es una
    ayuda que el asesor confirma. Es SEGURO llamarlo siempre:

      - Sin ANTHROPIC_API_KEY en el entorno -> devuelve {} (nada cambia).
      - Si una llamada al modelo falla -> se omite esa cuenta (se queda REVISAR).

    No se llama de forma automática en el pipeline para no introducir coste ni
    no-determinismo en el análisis base; el llamante decide cuándo activarlo.
    """
    # Import diferido: el módulo (y el SDK de anthropic) solo se cargan si se usa.
    from .engine import razonador

    if not razonador.hay_clave():
        return {}

    corte = _fecha_corte(libro) if libro is not None else None
    sugerencias: dict = {}
    for r in informe.resultados:
        if r.clasificacion != Clasificacion.REVISAR:
            continue
        corte_cuenta = corte or razonador.fecha_corte_de(r)
        try:
            sugerencias[r.codigo_cuenta] = razonador.razonar_cuenta(r, corte_cuenta)
        except razonador.SinClaveAPI:
            return {}  # dejó de haber clave: nada que sugerir
        except Exception:
            # Cualquier fallo del SDK/red: se omite esta cuenta, el motor manda.
            continue
    return sugerencias


def repasar_decididas(informe: Informe, fecha_corte: date | None = None) -> list:
    """Repaso de control de calidad sobre las cuentas que el motor YA decidió.

    Devuelve la lista de REPAROS (cuentas donde el razonador discrepa del
    motor); vacía si está de acuerdo con todo, si no hay clave, o si falla.
    Nunca cambia la clasificación: solo señala dónde mirar.
    """
    from .engine import razonador

    if not razonador.hay_clave():
        return []
    decididas = [r for r in informe.resultados if r.clasificacion in _DECIDIDAS]
    if not decididas:
        return []
    corte = fecha_corte or _fecha_corte_informe(informe)
    try:
        return razonador.repasar_cuentas(decididas, corte)
    except razonador.SinClaveAPI:
        return []
    except Exception:
        # Fallo de red/SDK/respuesta: el repaso es una ayuda opcional, no rompe.
        return []


def revisar_con_ia(
    informe_pagos: Informe,
    informe_facturas: Informe | None = None,
    libro: LibroMayor | None = None,
) -> dict:
    """Revisión completa con Claude de un análisis ya hecho (bajo demanda).

    Cubre los DOS motores, porque las cuentas en REVISAR no salen siempre del
    mismo: en los libros reales el de pagos-sin-factura no deja ninguna y todas
    las dudosas vienen del análisis inverso de facturas-sin-pago.

    Por cada motor hace dos pasadas:
      - `profundo`: razonamiento por cuenta sobre las que están en REVISAR.
      - `reparos` : repaso por lotes de las que el motor ya decidió.

    El motor sigue siendo la fuente de verdad: esto no reclasifica nada, solo
    propone. Sin ANTHROPIC_API_KEY devuelve `activo: False` y listas vacías.
    """
    from .engine import razonador

    if not razonador.hay_clave():
        return {"activo": False, "analisis": {}}

    corte = _fecha_corte(libro) if libro is not None else None
    salida: dict = {"activo": True, "analisis": {}}
    fuentes = [("pagos", informe_pagos), ("facturas", informe_facturas)]
    for nombre, informe in fuentes:
        if informe is None:
            continue
        corte_informe = corte or _fecha_corte_informe(informe)
        profundo: dict = {}
        for r in informe.resultados:
            if r.clasificacion != Clasificacion.REVISAR:
                continue
            try:
                profundo[r.codigo_cuenta] = razonador.razonar_cuenta(r, corte_informe)
            except Exception:
                # Fallo puntual del SDK/red: esa cuenta se queda como la dejó el
                # motor (REVISAR). Las demás siguen adelante.
                continue
        salida["analisis"][nombre] = {
            "profundo": profundo,
            "reparos": repasar_decididas(informe, corte_informe),
        }
    return salida
