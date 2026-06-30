# Detección de pagos sin factura — v1 (precision-first)

Primer módulo de una plataforma de análisis contable. El usuario sube un **Libro
Mayor (Fichas de Mayor)** en Excel o PDF y el sistema detecta, por cada cuenta de
proveedor/acreedor, **qué pagos no tienen una factura que los respalde**.

> **Requisito innegociable: cero falsos positivos.** Es una herramienta fiscal.
> El sistema **afirma solo lo que puede probar** y manda todo lo ambiguo a
> revisión humana. Precisión por encima de exhaustividad.

---

## Dos análisis (pestañas)

La webapp tiene dos pestañas sobre el mismo fichero:

1. **Pagos sin factura** (análisis directo): pagos sin factura que los respalde.
2. **Facturas sin pago** (análisis inverso, espejo): facturas sin ningún pago.
   - `FACTURA_SIN_PAGO` (alta confianza) = cuenta con facturas y **cero pagos**.
   - `REVISAR` = infrapagada (pago parcial); se da el importe pendiente, no la
     factura concreta (eso es v2).
   - `CONCILIADA` = pagada/sobrepagada o sin facturas.
   - Cada factura se lista con su **fecha, vencimiento y antigüedad** (aging por
     fecha de factura, o por vencimiento si consta; corte = última fecha del libro,
     determinista). Panel de antigüedad, PDF cliente propio y toggle «En informe».
   - Las `REVISAR` muestran **la razón** (sub-casilla: desfase de corte, distorsión
     por abono, pago parcial, deuda antigua) y un botón **«➕ Añadir al informe»**:
     por defecto NO entran en el PDF, pero al revisarlas y confirmar que son
     facturas sin pagar puedes incluirlas (`FACTURA_SIN_PAGO` entra por defecto y
     se puede ocultar). La inclusión la decide `incluir_en_informe_facturas`.
   - Aviso de dominio: una factura sin pagar suele ser deuda viva normal; la
     antigüedad señala lo relevante.

## Seguridad y despliegue

La app incluye **login obligatorio** (contraseñas con hash PBKDF2, sesiones firmadas
`HttpOnly`/`Secure`, cabeceras de seguridad, docs de API desactivadas en producción).

- **Local (dev)**: arranca sin configurar nada; usuario por defecto `admin`/`admin`.
- **Registro autoservicio**: la gente se crea su cuenta en **`/registro`** con un
  **único código de invitación, el mismo siempre** (por defecto `gestiona2026`;
  cámbialo con `GESTIONA_CODIGO_REGISTRO`). Así cada uno entra desde su dispositivo
  sin que tú crees nada, pero **solo quien tiene el código** puede registrarse. Las
  cuentas se guardan en `usuarios.db`.
- **Producción**: exige `GESTIONA_ENV=production`, `GESTIONA_SECRET_KEY` y un código
  de registro o usuarios admin (`python crear_usuario.py`). Ver **`.env.example`**.
- **Despliegue en tu dominio (Cloudflare)**: guía paso a paso en **`DEPLOY.md`**
  (Cloudflare Tunnel + Access + HTTPS, sin abrir puertos). Hay `Dockerfile`.

Crear/actualizar usuarios: `python crear_usuario.py`. Cerrar sesión: botón **Salir**.

## 1. Cómo ejecutar

```bash
cd backend
pip install -r requirements.txt

# Tests (incluye los obligatorios del §9 + integración contra los ficheros reales)
python -m pytest -q

# Servicio + UI
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8011
# -> abre http://127.0.0.1:8011
```

Arrastra el Excel (o PDF) a la página. Obtienes el resumen, la tabla de cuentas
con drill-down al detalle de cada apunte, los botones de confirmación humana y la
exportación a Excel/PDF.

---

## 2. Modelo de dominio

Cuentas **`400xxxx`** (Proveedores) y **`410xxxx`** (Acreedores). Por cada cuenta,
movimientos con Debe/Haber/Saldo:

| Concepto | Lado | Comentario | Encoding |
|---|---|---|---|
| Factura recibida | Haber | `Su Fra.: <num> <PROVEEDOR>` | `TipoFactura = R` |
| Pago | Debe | `Pago factura` | — |
| Abono / rectificativa | Haber **negativo** | `Su Fra.: ...` | reduce la deuda |

**Saldo contable de la cuenta = Σ Debe − Σ Haber.** En la muestra, el `SaldoActual`
de cada apunte es exactamente el acumulado de `Debe − Haber` (apertura = 0).

### Modelo canónico

Toda la ingesta produce una única estructura `Movimiento`
([`app/domain/models.py`](backend/app/domain/models.py)):

```
Movimiento(codigo_cuenta, nombre_cuenta, fecha, asiento, tipo[FACTURA|PAGO|ABONO|OTRO],
           debe: Decimal, haber: Decimal, comentario, referencias, orden, origen,
           saldo_reportado)
```

Los importes son `Decimal` (no `float`) para que el cálculo sea exacto y
**determinista**: mismo fichero → mismo resultado, siempre. El motor
([`app/engine/detector.py`](backend/app/engine/detector.py)) **solo** conoce este
modelo: ni pandas, ni ficheros, ni UI. Es una librería pura, testeable en aislamiento.

---

## 3. Lógica de detección (v1)

Se procesa **por cuenta**. **No se empareja pago↔factura por importe** (lo evitamos
a propósito: los pagos son agrupados, parciales o netos contra abonos, y el match
exacto genera decenas de falsos positivos).

1. **Filtrado.** Solo proveedores/acreedores (grupos **40 y 41** del PGC). El resto
   del libro mayor (clientes 43, tesorería 57, Hacienda 47, ingresos 70, gastos 6x…)
   queda **`FUERA_DE_ALCANCE`**: no son pagos a proveedores, así que no se analizan;
   se resumen aparte por grupo contable (sin alarma), NO se mezclan con las
   exclusiones técnicas. Se **excluye** además `4009xxx`/`4109xxx`
   (`FACTURAS PTES. RECIBIR`) y cuentas técnicas/puente. *(En la muestra, sin
   excluirla, 4009000 sería el mayor falso positivo: Σ Debe ≈ 8.607 €, Σ Haber = 0.)*
2. **Fiabilidad.** Se reconstruye el saldo (`apertura + Σ Debe − Σ Haber`) y se
   compara con `SaldoActual` (tolerancia ±0,02 €). Si no cuadra → **`NO_FIABLE`** y
   no se concluye nada.
3. **Aviso de apertura.** Si no hay saldo de apertura y hay pagos en el primer
   periodo del fichero, se baja la confianza (la factura podría ser de un ejercicio
   anterior no incluido).
4. **Clasificación a nivel cuenta** — lo único que se afirma:

| Clasificación | Condición | Se afirma |
|---|---|---|
| **`SIN_FACTURA_ALTA_CONFIANZA`** | hay pagos y **Σ Haber = 0** (cero facturas) | sí, con evidencia + verificación humana |
| **`REVISAR`** | sobrepagada (Σ Debe > Σ Haber) **pero con** facturas | **no** se afirma; se muestra para revisar |
| **`CONCILIADA`** | Σ Debe ≤ Σ Haber | sin alerta |
| **`NO_FIABLE`** | saldo reconstruido ≠ SaldoActual | no se concluye |
| **`EXCLUIDA`** | cuenta técnica / no proveedor | fuera del análisis |

Cada veredicto viaja **siempre con su motivo y su evidencia** (todos los apuntes de
la cuenta). Nunca una etiqueta suelta.

### Sub-casillas de `REVISAR` (triage, no afirmación)

`REVISAR` no se afirma nunca, pero **no todas las cuentas llegan ahí por el mismo
motivo**. Para entender cada caso y reducir falsos positivos, cada cuenta
sobrepagada se sub-clasifica de forma determinista según la **trayectoria de su
saldo** (¿llegó a estar a crédito alguna vez? ¿el último pago explica el débito?).
Ordenadas de **menor a mayor sospecha** de ser un pago realmente sin factura:

| Sub-casilla | Cuándo | Acción sugerida |
|---|---|---|
| **Factura posiblemente en otra cuenta** | La cuenta no tiene facturas, pero el **mismo proveedor** aparece en otra cuenta que sí las tiene | Comprobar la otra cuenta antes de concluir |
| **Crédito no identificado** | Hay Haber pero ninguna factura `Su Fra.` (p.ej. una reversión de pago) | Ver qué respalda ese crédito |
| **Distorsión por abono** | Hay rectificativas (Haber negativo) que empujan el exceso | Netear contra el abono |
| **Arrastre de ejercicio anterior** | La cuenta abre pagando y **nunca estuvo a crédito** | Comprobar el mayor del año anterior |
| **Desfase de corte** | Estuvo a crédito y el **último pago** explica el débito final | Revisar facturas posteriores al corte |
| **Sobrepago a revisar** | Débito que **no** se explica por apertura ni por el último pago | Revisar con prioridad (candidato más fuerte) |

En la UI hay un panel **"¿Por qué una cuenta cae en REVISAR?"** con el recuento y
el importe de cada sub-casilla; al pulsar una, la tabla se filtra a esas cuentas.
La sub-casilla y su evidencia viajan también en el export a Excel.

Sobre la muestra real, las 19 cuentas `REVISAR` se reparten así: 1 factura en otra
cuenta (43,94 €), 1 crédito no identificado (50 €), 7 arrastre (999,93 €), 8 desfase
de corte (1.900,51 €), 2 sobrepago (813,03 €).

#### Guardrail anti-falso-positivo: proveedor en otra cuenta

Una cuenta candidata a `SIN_FACTURA` se **degrada automáticamente a `REVISAR`** si el
mismo proveedor aparece en otra cuenta que sí tiene facturas (la factura podría estar
allí). El matcher es conservador —coincidencia por subconjunto de tokens distintivos,
ignorando sufijos societarios y palabras genéricas— para no ser él mismo una fuente
de ruido. En la muestra detecta `4000109 AMAZON EU (ITALIA)` → `4100091 AMAZON`.

Para estas cuentas, la UI muestra además un **indicador de factura candidata**: por
cada pago, la factura más probable. La confianza se calcula por **suma de señales**
(no por una sola variable), y se muestran las señales que casaron, para que el humano
audite. Señales:

| Señal | Qué corrobora |
|---|---|
| importe exacto | base (requisito) |
| mismo **NIF/CIF** | identidad fiscal (ignora NIF comodín tipo A99999999) |
| **nombre en la cuenta** | la factura está en una cuenta cuyo nombre es el proveedor |
| **nombre en el comentario** | `Su Fra.: <nº> <PROVEEDOR>` ↔ proveedor del pago (p.ej. `Recibo Naturgy…`) |
| referencia menciona proveedor | la ref de la factura nombra al proveedor |
| fecha próxima / importe único | corroboración de soporte |

Sin ninguna señal de identidad (solo importe+fecha) → **BAJA ("posible")**. Con
identidad y suficiente corroboración → **MEDIA/ALTA**. Busca en las cuentas del
proveedor y en las genéricas (ACREEDORES/PROVEEDORES VARIOS) en una sola pasada.
Es asistencia al revisor (no afirmación). En la muestra: el pago de 26,95 € de
`4000109` → factura `ES60000P6U1FHI` de `4100091` (26,95 €, 1 día antes), confianza
ALTA. El pago de 16,99 € no recibe candidata (su factura está en una cuenta genérica,
no en la del proveedor): se evita inventar.

### Resultado sobre la muestra real

7 cuentas `SIN_FACTURA_ALTA_CONFIANZA` (801,21 €, todas en confianza **MEDIA** por
ausencia de saldo de apertura), 19 en `REVISAR` (3.807,41 €), 22 conciliadas, 0 no
fiables, 1 excluida (4009000).

---

## 4. Arquitectura

```
Ingesta (Excel/PDF)  ->  Modelo canónico  ->  Motor de detección  ->  Reporte/UI
  parsers válidos        Movimiento           librería PURA           FastAPI + HTML
  contra totales         (Decimal)            (determinista)          export XLSX/PDF
```

- **`app/domain/`** — modelo canónico y estructuras de resultado.
- **`app/ingest/`** — `excel_parser` (autoritativo), `pdf_parser` (fallback),
  `clasificador` (comentario→tipo, compartido).
- **`app/engine/`** — motor puro de detección. **Sin I/O.**
- **`app/persistence/`** — overrides del humano en SQLite (base de la v2).
- **`app/reporting/`** — serialización + export Excel/PDF.
  - **Export PDF = informe profesional para el cliente** (`pdf_export.py`): cabecera
    de marca (wordmark *Gestiona más* en rojo corporativo) y SOLO los pagos sin
    factura **claros** (`SIN_FACTURA_ALTA_CONFIANZA`); las cuentas en REVISAR se
    omiten porque caen ahí por otros motivos. Cada pago con **fecha**, asiento,
    concepto e importe; resumen con nº de pagos y total. Para usar el logo real, deja
    `app/static/logo.png` (se incrusta automáticamente en vez del wordmark).
    Cada pago sin factura tiene en la UI un **toggle «✓ En informe / ✗ Oculto»**:
    el revisor decide qué pagos entran en el PDF del cliente. La decisión se
    persiste por (huella, cuenta) y el endpoint `export.pdf` la respeta.
  - El **export Excel** sigue siendo el detalle completo y auditable (todas las
    clasificaciones, sub-casillas y movimientos).
- **`app/api/`** — FastAPI fino que orquesta ingesta→motor→reporte.
- **`app/static/`** — UI de una página (carga, resumen, drill-down, overrides).

El humano confirma cada veredicto (**"sí, sin factura" / "está en otra cuenta" /
"es de 2025"**) y esos overrides se **persisten** por huella del fichero.

---

## 5. Supuestos

- El `SaldoActual` del Excel es la verdad de referencia para validar el parseo.
- El orden de los apuntes en el fichero es el orden contable (se preserva por
  índice de fila).
- Comentario `Su Fra.: ...` ⇒ lado Haber (factura); Haber negativo ⇒ abono.
  Comentario `Pago ...` ⇒ lado Debe.
- La señal autoritativa de "tiene facturas" es **Σ Haber ≠ 0**, no el conteo de
  comentarios (más conservador: si hay cualquier crédito, no se afirma sin factura).

## 6. Limitaciones conocidas

- **Factura en otra cuenta/epígrafe.** El sistema dice *"no encuentro factura
  **aquí**"*, no *"no existe factura"*. Por eso `SIN_FACTURA` exige confirmación
  humana.
- **Saldo de apertura ausente.** Si el fichero no trae sumas anteriores (como en la
  muestra, todo a 0), los pagos del primer periodo pueden liquidar facturas de un
  ejercicio anterior. Se avisa con banner y se baja la confianza a `MEDIA`.
- **Fiabilidad del PDF.** El PDF colapsa Debe/Haber en una sola columna; la
  extracción es frágil. Se valida cada cuenta contra su línea `Suma Movimientos` y,
  vía reconstrucción de saldo, un parseo malo acaba en `NO_FIABLE`, **nunca** en una
  afirmación. **Si existe el Excel, se usa el Excel.**

## 7. Camino a la v2

La v1 es deliberadamente conservadora: el cubo `REVISAR` agrupa todo lo que no se
puede afirmar sin riesgo. La v2 **no cambia la corrección**, mejora la
**exhaustividad** reduciendo `REVISAR` con **emparejamiento fino**:

- subset-sum pago↔facturas (pagos agrupados),
- pagos parciales / a cuenta,
- netos contra abonos,
- arrastre entre ejercicios.

Los **overrides persistidos** del humano (tabla `overrides`) son la materia prima
para entrenar y validar ese emparejamiento.

---

## 8. Guardrails (lo que el sistema NO hace)

- No afirma "sin factura" salvo en cuentas con **cero** facturas, y aun así para
  verificación.
- No empareja pago↔factura por importe en v1.
- No usa ningún LLM en la ruta de cálculo en runtime (motor Python determinista).
- No se fía del PDF si existe el Excel.
- No concluye sobre cuentas cuyo saldo reconstruido no cuadra.
- No procesa cuentas técnicas (`4009xxx`) como proveedores.
- No trata pagos del primer periodo como huérfanos sin avisar de la apertura ausente.

## 9. Tests

`python -m pytest -q` — 17 tests. Incluye los obligatorios del §9 del encargo:
pagos sin facturas → `SIN_FACTURA`; sobrepagada con muchas facturas → `REVISAR`
(nunca afirmada); abonos + pagos netos → 0 afirmadas; `4009000` → excluida; saldo
que no cuadra → `NO_FIABLE`; determinismo; + integración contra los ficheros reales.
