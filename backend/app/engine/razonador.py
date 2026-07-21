"""Razonador de casos REVISAR con la API de Claude (Opus 4.8).

Segunda capa, NO sustituye al motor determinista:

    Parseo  ->  MotorDeteccion (determinista)  ->  Razonador (LLM) sobre REVISAR

El motor sigue siendo la fuente de verdad para todo lo que se PUEDE probar
aritméticamente (saldo a cero, exceso neto sobre lo facturado, etc.). El
razonador solo mira las cuentas que el motor dejó en REVISAR —las que hoy hay
que "pintar" a mano— y aplica el CRITERIO HUMANO que describe el audio del
proceso: distinguir un descuadre reciente y explicable (no alarma) de un
problema ANTIGUO real, entendiendo aperturas, arrastres y desfases de corte.

Diseño (idéntico patrón que reporting/report_chat.py):
  - Si NO hay ANTHROPIC_API_KEY en el entorno, se lanza `SinClaveAPI` y el
    llamante se queda con el veredicto REVISAR del motor (nunca peor que hoy).
  - Salida ESTRUCTURADA (json_schema) para que el modelo devuelva exactamente
    los campos que esperamos, encajados en el vocabulario del dominio.
  - Pensamiento adaptativo (`thinking: adaptive`) + `effort` para razonamiento
    de varios pasos con calidad.
  - El razonador PROPONE (sugerencia + motivo + confianza); el veredicto que ve
    el cliente lo confirma el asesor. Así se mantiene el "cero falsos positivos".

La clave se lee del entorno (config.py carga un `.env` de la raíz). El código
NUNCA contiene la clave.

Dos niveles de revisión (los dos bajo demanda, nunca en el análisis base):

  1. REPASO (barato, por lotes): pasa por las cuentas que el motor YA decidió
     (CONCILIADA, SIN_FACTURA_ALTA_CONFIANZA, FACTURA_SIN_PAGO) y solo señala
     aquellas donde la decisión le chirría. Una llamada por lote, no por cuenta.
  2. PROFUNDO (caro, por cuenta): `razonar_cuenta` sobre las que están en
     REVISAR, que son las que hoy hay que pintar a mano.

Configuración por variables de entorno:
  ANTHROPIC_API_KEY                (obligatoria para activar la capa)
  GESTIONA_RAZONADOR_MODELO        (por defecto "claude-opus-4-8")
  GESTIONA_RAZONADOR_EFFORT        (profundo; por defecto "high")
  GESTIONA_RAZONADOR_EFFORT_REPASO (repaso; por defecto "medium")
  GESTIONA_RAZONADOR_LOTE          (cuentas por llamada en el repaso; 20)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache

from ..domain.models import SubcategoriaRevisar, TipoMovimiento
from ..domain.resultados import ResultadoCuenta

# Modelo por defecto: el más capaz en razonamiento. Override con la variable de
# entorno GESTIONA_RAZONADOR_MODELO.
MODELO = os.getenv("GESTIONA_RAZONADOR_MODELO", "claude-opus-4-8")
# Profundidad de razonamiento (coste/calidad). Override con GESTIONA_RAZONADOR_EFFORT.
EFFORT = os.getenv("GESTIONA_RAZONADOR_EFFORT", "high")
# El repaso mira muchas cuentas ya decididas: menos esfuerzo por cuenta.
EFFORT_REPASO = os.getenv("GESTIONA_RAZONADOR_EFFORT_REPASO", "medium")
# Cuentas por llamada en el repaso (control de coste y de tamaño de contexto).
LOTE = max(1, int(os.getenv("GESTIONA_RAZONADOR_LOTE", "20")))

# Techo de tokens por llamada. OJO: con thinking adaptativo el razonamiento y la
# salida COMPARTEN este presupuesto; si se queda corto, el JSON se trunca
# (stop_reason "max_tokens") y la cuenta se pierde. Por eso es holgado: es un
# tope, no lo que se factura (se paga lo realmente usado).
MAX_TOKENS = int(os.getenv("GESTIONA_RAZONADOR_MAX_TOKENS", "16000"))
MAX_TOKENS_REPASO = int(os.getenv("GESTIONA_RAZONADOR_MAX_TOKENS_REPASO", "16000"))

# Colchón por defecto (días) por debajo del cual un descuadre se considera
# reciente y sin alarma. Es una guía para el modelo, no un corte rígido.
COLCHON_DIAS = int(os.getenv("GESTIONA_RAZONADOR_COLCHON_DIAS", "60"))

# Veredictos que puede sugerir el razonador (vocabulario propio, legible para el
# asesor; NO pisa la Clasificacion del motor).
VEREDICTOS = ("NO_ALARMA", "SOSPECHA_ANTIGUA", "INCIERTO")
CONFIANZAS = ("ALTA", "MEDIA", "BAJA")


class SinClaveAPI(RuntimeError):
    """No hay ANTHROPIC_API_KEY configurada en el entorno."""


@dataclass(frozen=True)
class SugerenciaRazonador:
    """Segunda opinión razonada sobre una cuenta en REVISAR.

    Es una SUGERENCIA con evidencia, no una afirmación: el asesor confirma.
    """

    codigo_cuenta: str
    veredicto: str            # uno de VEREDICTOS
    subcategoria: str | None  # un valor de SubcategoriaRevisar, o None
    antiguedad_dias: int | None  # antigüedad del apunte problemático más viejo
    reciente_sin_alarma: bool    # dentro del colchón (p.ej. factura del mes en curso)
    motivo: str               # explicación en lenguaje del asesor
    confianza: str            # uno de CONFIANZAS

    @property
    def dar_por_bueno(self) -> bool:
        """True si el razonador cree que la cuenta se puede "pintar" como buena."""
        return self.veredicto == "NO_ALARMA"


# Bloque compartido por los dos prompts: la aritmética de una cuenta de
# proveedor y el invariante del signo del saldo. Es lo que evita las dos
# familias de falso positivo que ya se han visto en libros reales.
_LECTURA_CUENTA = """\
# CÓMO SE LEE UNA CUENTA DE PROVEEDOR

Una cuenta de proveedor es una cuenta de PASIVO:

- HABER (crédito) = FACTURA recibida. Aumenta lo que debemos al proveedor.
- DEBE  (débito)  = PAGO al proveedor. Reduce lo que le debemos.
- ABONO / rectificativa = Haber negativo. Reduce la deuda sin ser un pago.
- El saldo normal es acreedor (Haber > Debe): le debemos dinero.

SALDO A CERO = TODO CASADO. Si al recorrer la cuenta el saldo vuelve a cero,
los apuntes que hay entre medias se corresponden entre sí, aunque no encajen
uno a uno. Tal y como lo dice el asesor: "tengo una factura, tengo un abono y
tengo un pago de diferencia; como el saldo da cero, esas dos corresponden a
este pago". No busques emparejamientos 1:1 donde el saldo ya te dice que está
cuadrado.

# LA REGLA DEL SIGNO (invariante duro, no lo violes nunca)

Sea exceso_neto = Σ Debe − Σ Haber.

- exceso_neto > 0 (SOBREPAGADA): se ha pagado más de lo facturado. Solo aquí
  puede existir un pago sin factura, y como MUCHO por el importe del exceso
  neto. Ni un céntimo más es afirmable: cualquier cantidad por encima del
  exceso está respaldada por alguna factura de la propia cuenta.
- exceso_neto < 0 (INFRAPAGADA): se ha facturado más de lo pagado. Es
  ARITMÉTICAMENTE IMPOSIBLE que haya un pago sin factura. Si la cuenta que
  estás mirando está infrapagada y te tienta decir "hay un pago huérfano",
  estás equivocado: ese pago tiene factura en la propia cuenta. El sitio de
  esta cuenta es el análisis inverso (facturas pendientes de pago), no este.
- exceso_neto ≈ 0 (±3 €): cuadrada. Las diferencias de céntimos son redondeos.
"""

# Los seis patrones de oficio que explican casi todos los descuadres. Salidos
# del audio del asesor y de los casos reales ya resueltos.
_PATRONES = """\
# LOS SEIS PATRONES QUE LO EXPLICAN CASI TODO

Antes de señalar nada, comprueba si el caso encaja en uno de estos. Si encaja,
NO es alarma.

1. SALDO DE APERTURA AL DEBE (arrastre de ejercicio anterior)
   La cuenta "abre pagando": el primer apunte es un pago, o hay un saldo de
   apertura en el Debe, y la cuenta NUNCA estuvo a crédito antes.
   Qué significa: ese pago liquida una factura de un ejercicio ANTERIOR que no
   está en este fichero. No es un pago sin factura.
   Señal fuerte: el importe descuadrado COINCIDE con el de la apertura.
   Conclusión correcta: "revisar el Mayor del año anterior" — literalmente, el
   asesor llama al cliente y le dice "oye, revisa el año anterior, tienes un
   importe que se te ha quedado de antes".
   → NO_ALARMA, subcategoría ARRASTRE_EJERCICIO_ANTERIOR.

2. DESFASE DE CORTE
   El banco ya movió el dinero (hay pago) pero la factura entra el mes
   siguiente, después del corte del fichero. El asesor: "he contabilizado el
   pago de julio y falta la factura de julio; pero también está bien, porque
   estamos a fecha".
   → NO_ALARMA, subcategoría DESFASE_DE_CORTE.

3. PAGO PARCIAL / ANTICIPO
   Una factura se paga en varios pagos. Ejemplo real del asesor: factura de
   1.359,28 €, se pagaron 1.350 € y luego la diferencia. Ninguno de los dos
   pagos casa 1:1 con la factura, pero juntos sí y el saldo queda a cero.
   → NO_ALARMA.

4. PAGO AGRUPADO
   Un solo pago liquida varias facturas ("FACTURAS ABRIL Y MAYO", 496,98 = suma
   de varias). El pago no casa con ninguna factura individual.
   → NO_ALARMA.

5. ABONO / FACTURA RECTIFICATIVA
   Hay un abono que reduce la deuda sin ser un pago. El "sobrepago" aparente es
   un artefacto: hay que netear factura − abono = pago.
   → NO_ALARMA, subcategoría DISTORSION_POR_ABONO. Pero si el abono deja
   genuinamente confuso qué queda pendiente, INCIERTO es una respuesta honesta.

6. FACTURA EN OTRA CUENTA
   El mismo proveedor tiene otra cuenta en el libro donde sí están sus facturas
   (típico: una cuenta genérica tipo VARIOS y otra nominativa).
   → NO_ALARMA o INCIERTO, subcategoría FACTURA_EN_OTRA_CUENTA, indicando que
   hay que comprobar la otra cuenta antes de concluir.
"""

# El caso ARTRIP·INTEGRATED: falso positivo real (3.344,71 € afirmados en una
# cuenta infrapagada). Se cita literalmente para anclar la regla.
_CERO_FALSOS_POSITIVOS = """\
# CERO FALSOS POSITIVOS (la regla que gobierna todas las demás)

Afirmar "aquí hay dinero pagado sin factura" es una ACUSACIÓN. Si es falsa, la
asesoría queda mal delante de su cliente. Un falso positivo cuesta
infinitamente más que un falso negativo.

Caso real que costó un disgusto, no lo repitas: una cuenta con pagos parciales
(334,11 + 603,64 = 937,75 correspondientes a una misma factura) fue declarada
con 3.344,71 € "sin factura" cuando la cuenta en NETO estaba infrapagada en
1.200,84 €. Dos errores a la vez: ignorar los parciales y saltarse la regla del
signo. La respuesta correcta era: no hay nada que reclamar aquí.
"""

_SISTEMA = ("""\
# IDENTIDAD

Eres un asesor contable senior español, especialista en revisión de cuentas de
PROVEEDORES y ACREEDORES (grupos 40 y 41 del PGC). Llevas años haciendo a mano
exactamente esta tarea: abrir la Ficha de Mayor de un proveedor, recorrer los
apuntes de arriba abajo e ir "pintando" los que casan entre sí, hasta quedarte
solo con lo que no cuadra.

Tu cliente es una asesoría que analiza los libros de SUS clientes. Lo que tú
digas acaba, filtrado por un asesor humano, en un informe que se le entrega al
cliente final.

# TU SITIO EN EL PROCESO

El análisis tiene tres capas y tú eres la tercera:

  1. PARSEO: el Libro Mayor (Excel o PDF) se convierte en apuntes normalizados.
  2. MOTOR DETERMINISTA: casa pagos con facturas por aritmética exacta
     (subset-sum con tolerancia de 3 €, pagos agrupados, pagos parciales,
     abonos, arrastres de apertura). Es la fuente de verdad de todo lo
     demostrable. Cierra la inmensa mayoría de las cuentas solo.
  3. TÚ: recibes ÚNICAMENTE las cuentas que el motor NO ha podido resolver con
     certeza (estado REVISAR). Son las que hoy un humano tiene que mirar a mano.

Consecuencias que debes tener presentes:

- No estás para recalcular sumas: el motor ya lo hizo y sus números son
  exactos. Estás para aportar el CRITERIO que la aritmética no captura: ¿esto
  es un problema real, o tiene una explicación normal de oficio?
- Nunca ves las cuentas fáciles. Que un caso llegue a ti no significa que sea
  sospechoso; significa que es ambiguo.
- Tu salida es una SUGERENCIA con evidencia, no un veredicto. El asesor humano
  confirma antes de que llegue al cliente.

"""
+ _LECTURA_CUENTA
+ """
# EL CRITERIO: LA ANTIGÜEDAD MANDA

Regla de oficio, dicha por el asesor: "todas las diferencias que tenemos que
recoger en ese informe son anteriores; mínimo 30 días, porque nosotros mínimo
lo que necesitamos... están trabajando la contabilidad. No necesito que esto se
me llene de cosas que están bien".

- Un descuadre RECIENTE (dentro de ~{colchon} días de la fecha de corte) NO es
  alarma. La contabilidad del periodo todavía se está cerrando: la factura
  puede estar por llegar, o el pago por hacerse.
- Ejemplo literal del asesor: con corte a 18/07, un apunte del 09/07 "le
  daríamos por bueno, no pasa nada". Nueve días no son un problema, son trabajo
  en curso.
- "Todavía se puede pagar... podría pasar hasta 60 días": una factura sin pagar
  dentro del plazo comercial normal no es una incidencia.
- Lo que preocupa es lo ANTIGUO sin explicación, especialmente +90 días.

Si dudas entre "reciente" y "antiguo", mira antiguedad_dias del apunte
problemático más viejo, no la fecha de la cuenta en conjunto.

"""
+ _PATRONES
+ "\n"
+ _CERO_FALSOS_POSITIVOS
+ """
En la práctica:
- Ante duda genuina, INCIERTO. Nunca inventes una alarma para parecer útil.
- Ante un patrón de los seis anteriores, NO_ALARMA aunque un apunte suelto no
  case 1:1.
- SOSPECHA_ANTIGUA solo si: es antiguo, está por encima del colchón, NO encaja
  en ninguno de los seis patrones, y puedes decir en una frase concreta qué
  importe y qué fecha no tienen explicación.

# VEREDICTOS

- NO_ALARMA: reciente (dentro del colchón) o explicable por uno de los seis
  patrones. El asesor puede darla por buena y no aparecerá en el informe.
- SOSPECHA_ANTIGUA: descuadre antiguo y sin explicación. Dinero pagado sin
  factura que respalde, o factura vencida sin pagar. Esto es lo que el asesor
  quiere ver: es prioridad y llega al cliente.
- INCIERTO: los datos del fichero no bastan para decidir. Requiere comprobación
  manual. Es una respuesta legítima y valiosa, no un fracaso.

# FORMATO DE SALIDA

- 'veredicto': NO_ALARMA | SOSPECHA_ANTIGUA | INCIERTO.
- 'subcategoria': la etiqueta del dominio que mejor explique el caso, o null.
- 'antiguedad_dias': antigüedad, en días respecto a la fecha de corte, del
  apunte PROBLEMÁTICO más antiguo (no del más antiguo de la cuenta). null si no
  aplica.
- 'reciente_sin_alarma': true solo si el descuadre cae dentro del colchón.
- 'motivo': 1-3 frases en español, como se las dirías a un compañero de
  despacho que va a coger el teléfono para llamar al cliente. Cita SIEMPRE
  importes y fechas concretos. Di qué ves y qué hay que hacer. Nada de lenguaje
  corporativo ni de repetir la etiqueta: "El pago de 827,06 € del 12/03 no
  tiene ninguna factura que lo respalde y la cuenta abre a cero; hay que
  reclamar el justificante" es útil. "Se detecta una anomalía en la
  conciliación" no lo es.
- 'confianza': ALTA | MEDIA | BAJA sobre tu propio juicio.
""").format(colchon=COLCHON_DIAS)


_SISTEMA_REPASO = (
    "Eres un asesor contable senior haciendo un REPASO de control de calidad "
    "sobre el trabajo de un motor automático que analiza el Libro Mayor de "
    "cuentas de PROVEEDORES y ACREEDORES (grupos 40 y 41 del PGC español).\n\n"
    "Te paso un lote de cuentas que el motor YA ha decidido, cada una con su "
    "clasificación, su motivo y sus apuntes. Tu trabajo NO es re-analizarlas "
    "una por una a fondo: es detectar las que huelen mal, es decir, aquellas "
    "donde la decisión del motor parece equivocada a la vista de los apuntes.\n\n"
    "Significado de cada clasificación:\n"
    "- CONCILIADA: el motor dice que no hay nada que reclamar aquí.\n"
    "- SIN_FACTURA_ALTA_CONFIANZA: el motor AFIRMA al cliente que hay dinero "
    "pagado sin factura que lo respalde. Es una acusación: un falso positivo "
    "aquí hace quedar mal a la asesoría delante de su cliente.\n"
    "- FACTURA_SIN_PAGO: el motor afirma que hay facturas pendientes de pago.\n\n"
    "Dónde mirar con más cuidado:\n"
    "1) Afirmaciones (SIN_FACTURA_ALTA_CONFIANZA, FACTURA_SIN_PAGO) que se "
    "expliquen por un pago agrupado, un pago parcial, un abono/rectificativa, "
    "un saldo de apertura o un desfase de corte. Si hay explicación, el motor "
    "no debería estar afirmando: eso es un FALSO POSITIVO y es lo más grave.\n"
    "2) CONCILIADA que tape algo evidente: un descuadre antiguo y sin "
    "explicación que debería haberse levantado (falso negativo).\n"
    "3) Un motivo del motor que no encaje con los apuntes que ves.\n\n"
    + _LECTURA_CUENTA
    + "\n"
    + _CERO_FALSOS_POSITIVOS
    + "\nUsa la regla del signo como filtro rápido: una cuenta INFRAPAGADA "
    "clasificada como SIN_FACTURA_ALTA_CONFIANZA es un error seguro del motor, "
    "no una duda. Y antes de discrepar de una afirmación, comprueba si los "
    "apuntes se explican por un pago agrupado, un pago parcial, un abono, un "
    "saldo de apertura o un desfase de corte.\n\n"
    "Reglas de salida (IMPORTANTES):\n"
    "- Devuelve una entrada por CADA cuenta del lote, con su mismo "
    "'codigo_cuenta'.\n"
    "- 'de_acuerdo': true si la decisión del motor te parece razonable. Sé "
    "conservador: por defecto true. Marca false SOLO si tienes un motivo "
    "concreto y verbalizable mirando los apuntes.\n"
    "- 'duda': si de_acuerdo es false, 1-2 frases diciendo qué no cuadra y qué "
    "habría que comprobar, citando importes y fechas. Si de_acuerdo es true, "
    "devuelve null.\n"
    "- 'confianza': ALTA/MEDIA/BAJA sobre tu propio juicio.\n"
    "- No inventes ruido: un repaso que marca la mitad del lote no sirve."
)


@dataclass(frozen=True)
class RevisionRepaso:
    """Reparo del razonador sobre una cuenta que el motor ya había decidido."""

    codigo_cuenta: str
    clasificacion_motor: str
    de_acuerdo: bool
    duda: str
    confianza: str


def _sistema(texto: str) -> list[dict]:
    """El prompt de sistema como bloque cacheable.

    Los dos prompts pasan de 1.024 tokens (mínimo cacheable), y un análisis
    hace una llamada por cuenta en REVISAR con el MISMO sistema: a partir de la
    segunda, el prefijo se lee de caché a una fracción del coste. La caché
    `ephemeral` dura 5 minutos y se renueva en cada uso, así que cubre de sobra
    un análisis completo.
    """
    return [{"type": "text", "text": texto, "cache_control": {"type": "ephemeral"}}]


def _subcategorias() -> list[str]:
    return [s.value for s in SubcategoriaRevisar]


def _esquema_repaso() -> dict:
    return {
        "type": "object",
        "properties": {
            "revisiones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "codigo_cuenta": {"type": "string"},
                        "de_acuerdo": {"type": "boolean"},
                        "duda": {"type": ["string", "null"]},
                        "confianza": {"type": "string", "enum": list(CONFIANZAS)},
                    },
                    "required": [
                        "codigo_cuenta", "de_acuerdo", "duda", "confianza",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["revisiones"],
        "additionalProperties": False,
    }


def _esquema() -> dict:
    return {
        "type": "object",
        "properties": {
            "veredicto": {"type": "string", "enum": list(VEREDICTOS)},
            # OJO: la API rechaza `null` DENTRO de un enum ("Enum value None
            # does not match declared type"), y también un enum de strings
            # sobre `type: [string, null]`. La forma que sí acepta para un
            # enum opcional es anyOf(null, enum).
            "subcategoria": {
                "anyOf": [
                    {"type": "null"},
                    {"type": "string", "enum": _subcategorias()},
                ],
            },
            "antiguedad_dias": {"type": ["integer", "null"]},
            "reciente_sin_alarma": {"type": "boolean"},
            "motivo": {"type": "string"},
            "confianza": {"type": "string", "enum": list(CONFIANZAS)},
        },
        "required": [
            "veredicto",
            "subcategoria",
            "antiguedad_dias",
            "reciente_sin_alarma",
            "motivo",
            "confianza",
        ],
        "additionalProperties": False,
    }


@lru_cache(maxsize=1)
def _cliente():
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SinClaveAPI()
    from anthropic import Anthropic  # import diferido: el módulo carga sin el SDK

    return Anthropic()


def fecha_corte_de(resultado: ResultadoCuenta) -> date | None:
    """Fecha de corte estimada: el último apunte con fecha de la cuenta.

    Si el orquestador conoce la fecha de corte real del libro (máximo de todas
    las cuentas), es preferible pasarla explícitamente a `razonar_cuenta`.
    """
    fechas = [m.fecha for m in resultado.movimientos if m.fecha is not None]
    return max(fechas) if fechas else None


def _num(d: Decimal) -> float:
    return float(d)


def _serializar_cuenta(resultado: ResultadoCuenta, fecha_corte: date | None) -> dict:
    """Vista compacta y neutra de la cuenta para el modelo."""
    movs = []
    for m in resultado.movimientos:
        dias = None
        if m.fecha is not None and fecha_corte is not None:
            dias = (fecha_corte - m.fecha).days
        movs.append(
            {
                "fecha": m.fecha.isoformat() if m.fecha else None,
                "antiguedad_dias": dias,
                "tipo": m.tipo.value,
                "debe": _num(m.debe),
                "haber": _num(m.haber),
                "asiento": m.asiento or None,
                "concepto": (m.comentario or "")[:160],
                "su_factura": m.referencias.su_factura,
                "nif": m.referencias.nif,
            }
        )
    return {
        "codigo_cuenta": resultado.codigo_cuenta,
        "nombre_cuenta": resultado.nombre_cuenta,
        "fecha_corte": fecha_corte.isoformat() if fecha_corte else None,
        "colchon_dias": COLCHON_DIAS,
        "saldo_debe_total": _num(resultado.suma_debe),
        "saldo_haber_total": _num(resultado.suma_haber),
        "saldo_reconstruido": _num(resultado.saldo_reconstruido),
        "num_facturas": resultado.num_facturas,
        "num_pagos": resultado.num_pagos,
        "num_abonos": resultado.num_abonos,
        "subcategoria_motor": resultado.subcategoria,
        "motivo_motor": resultado.motivo,
        "flags": list(resultado.flags),
        "movimientos": movs,
    }


#: Apuntes que se mandan por cuenta en el REPASO. El repaso solo busca el olor
#: a decisión equivocada; para el detalle completo está el análisis profundo.
MAX_MOVS_REPASO = 40


def _serializar_compacta(resultado: ResultadoCuenta, fecha_corte: date | None) -> dict:
    """Vista reducida para el repaso por lotes: decisión del motor + evidencia
    suficiente para olerse un error, sin gastar el contexto de un lote entero."""
    movs = []
    for m in resultado.movimientos[:MAX_MOVS_REPASO]:
        dias = None
        if m.fecha is not None and fecha_corte is not None:
            dias = (fecha_corte - m.fecha).days
        movs.append(
            {
                "fecha": m.fecha.isoformat() if m.fecha else None,
                "antiguedad_dias": dias,
                "tipo": m.tipo.value,
                "debe": _num(m.debe),
                "haber": _num(m.haber),
                "concepto": (m.comentario or "")[:80],
            }
        )
    datos = {
        "codigo_cuenta": resultado.codigo_cuenta,
        "nombre_cuenta": resultado.nombre_cuenta,
        "clasificacion_motor": resultado.clasificacion.value,
        "confianza_motor": resultado.confianza.value,
        "motivo_motor": resultado.motivo,
        "saldo_debe_total": _num(resultado.suma_debe),
        "saldo_haber_total": _num(resultado.suma_haber),
        "num_facturas": resultado.num_facturas,
        "num_pagos": resultado.num_pagos,
        "num_abonos": resultado.num_abonos,
        "flags": list(resultado.flags),
        "movimientos": movs,
    }
    if len(resultado.movimientos) > MAX_MOVS_REPASO:
        datos["aviso"] = (
            f"Se muestran {MAX_MOVS_REPASO} de {len(resultado.movimientos)} apuntes."
        )
    # Solo si aportan (análisis de pagos vs. análisis inverso de facturas).
    if resultado.importe_sospechoso:
        datos["importe_sospechoso"] = _num(resultado.importe_sospechoso)
    if resultado.importe_pendiente_pago:
        datos["importe_pendiente_pago"] = _num(resultado.importe_pendiente_pago)
    return datos


def repasar_cuentas(
    resultados: list[ResultadoCuenta],
    fecha_corte: date | None = None,
) -> list[RevisionRepaso]:
    """Repaso de control de calidad sobre cuentas YA decididas por el motor.

    Una llamada por lote de `LOTE` cuentas (no una por cuenta). Devuelve SOLO
    los reparos: las cuentas donde el razonador NO está de acuerdo con el motor.
    Las cuentas conformes no generan salida (no son noticia).

    Lanza `SinClaveAPI` si no hay clave. Un fallo de un lote concreto lo debe
    capturar el llamante: los demás lotes siguen siendo válidos.
    """
    if not resultados:
        return []
    cliente = _cliente()
    por_codigo = {r.codigo_cuenta: r for r in resultados}
    reparos: list[RevisionRepaso] = []

    for inicio in range(0, len(resultados), LOTE):
        lote = resultados[inicio:inicio + LOTE]
        contexto = json.dumps(
            [_serializar_compacta(r, fecha_corte) for r in lote],
            ensure_ascii=False,
        )
        resp = cliente.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS_REPASO,
            thinking={"type": "adaptive"},
            output_config={
                "effort": EFFORT_REPASO,
                "format": {"type": "json_schema", "schema": _esquema_repaso()},
            },
            system=_sistema(_SISTEMA_REPASO),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Repasa estas {len(lote)} cuentas ya decididas por el "
                        f"motor y dime en cuáles no estás de acuerdo.\n\n"
                        + contexto
                    ),
                }
            ],
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            raise RuntimeError("El modelo rechazó la petición (refusal).")
        if getattr(resp, "stop_reason", None) == "max_tokens":
            # Truncado: el JSON del lote queda a medias. Mejor fallar claro que
            # dejar que json.loads reviente con un error ilegible. Sube
            # GESTIONA_RAZONADOR_MAX_TOKENS_REPASO o baja el LOTE.
            raise RuntimeError(
                f"Respuesta truncada (max_tokens={MAX_TOKENS_REPASO}) en un lote "
                f"de {len(lote)} cuentas. Sube MAX_TOKENS_REPASO o baja el LOTE."
            )
        texto = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"), ""
        )
        datos = json.loads(texto or "{}")
        reparos.extend(_reparos_validos(datos, por_codigo))
    return reparos


def _reparos_validos(datos: dict, por_codigo: dict) -> list[RevisionRepaso]:
    """Filtra la respuesta del lote: solo desacuerdos, y solo de cuentas reales.

    El modelo devuelve una entrada por cuenta; aquí nos quedamos con las que
    marcan `de_acuerdo: false` y traen una duda verbalizada. Se ignoran códigos
    inventados o que no estaban en el lote.
    """
    salida: list[RevisionRepaso] = []
    for item in datos.get("revisiones") or []:
        if not isinstance(item, dict):
            continue
        codigo = item.get("codigo_cuenta")
        original = por_codigo.get(codigo)
        if original is None:          # código que no estaba en el lote: se ignora
            continue
        if item.get("de_acuerdo") is not False:
            continue                  # conforme: no es noticia
        duda = (item.get("duda") or "").strip()
        if not duda:                  # desacuerdo sin motivo: no es accionable
            continue
        conf = item.get("confianza")
        salida.append(
            RevisionRepaso(
                codigo_cuenta=codigo,
                clasificacion_motor=original.clasificacion.value,
                de_acuerdo=False,
                duda=duda,
                confianza=conf if conf in CONFIANZAS else "BAJA",
            )
        )
    return salida


def razonar_cuenta(
    resultado: ResultadoCuenta,
    fecha_corte: date | None = None,
) -> SugerenciaRazonador:
    """Segunda opinión razonada sobre una cuenta en REVISAR.

    Lanza `SinClaveAPI` si no hay clave, o el error del SDK si la llamada falla.
    El llamante debe capturarlos y quedarse con el veredicto del motor.
    """
    if fecha_corte is None:
        fecha_corte = fecha_corte_de(resultado)

    cliente = _cliente()
    contexto = json.dumps(
        _serializar_cuenta(resultado, fecha_corte), ensure_ascii=False, indent=None
    )

    resp = cliente.messages.create(
        model=MODELO,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={
            "effort": EFFORT,
            "format": {
                "type": "json_schema",
                "schema": _esquema(),
            },
        },
        system=_sistema(_SISTEMA),
        messages=[
            {
                "role": "user",
                "content": (
                    "Revisa esta cuenta de proveedor/acreedor y da tu veredicto.\n\n"
                    + contexto
                ),
            }
        ],
    )

    # Rechazo del modelo (seguridad): sin sugerencia -> el motor manda.
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("El modelo rechazó la petición (refusal).")
    if getattr(resp, "stop_reason", None) == "max_tokens":
        # Con thinking adaptivo el razonamiento se comió el presupuesto y el JSON
        # quedó truncado. Fallamos claro (el llamante se queda con REVISAR) en vez
        # de dejar que json.loads lance un error críptico. Sube MAX_TOKENS.
        raise RuntimeError(
            f"Respuesta truncada (max_tokens={MAX_TOKENS}) para la cuenta "
            f"{resultado.codigo_cuenta}. Sube GESTIONA_RAZONADOR_MAX_TOKENS."
        )

    texto = next(
        (b.text for b in resp.content if getattr(b, "type", None) == "text"), ""
    )
    datos = json.loads(texto or "{}")
    return _sugerencia_valida(resultado.codigo_cuenta, datos)


def _sugerencia_valida(codigo: str, datos: dict) -> SugerenciaRazonador:
    """Normaliza y valida la respuesta del modelo a nuestro dominio."""
    veredicto = datos.get("veredicto")
    if veredicto not in VEREDICTOS:
        veredicto = "INCIERTO"
    sub = datos.get("subcategoria")
    if sub is not None and sub not in {s.value for s in SubcategoriaRevisar}:
        sub = None
    conf = datos.get("confianza")
    if conf not in CONFIANZAS:
        conf = "BAJA"
    ant = datos.get("antiguedad_dias")
    ant = int(ant) if isinstance(ant, (int, float)) else None
    return SugerenciaRazonador(
        codigo_cuenta=codigo,
        veredicto=veredicto,
        subcategoria=sub,
        antiguedad_dias=ant,
        reciente_sin_alarma=bool(datos.get("reciente_sin_alarma")),
        motivo=(datos.get("motivo") or "").strip(),
        confianza=conf,
    )


def hay_clave() -> bool:
    """True si el razonador está activo (hay ANTHROPIC_API_KEY en el entorno)."""
    return bool(os.getenv("ANTHROPIC_API_KEY"))
