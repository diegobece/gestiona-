"""Banco de pruebas del razonador (capa LLM sobre REVISAR).

Corre el análisis determinista sobre un Libro Mayor real y luego pasa las
cuentas por Claude (Opus 4.8), imprimiendo cada sugerencia. Es la herramienta
para "entrenar" el razonador: afinar los prompts de razonador.py mirando qué
dice el modelo sobre cuentas reales, iterar, repetir.

A diferencia del endpoint /revisar-ia (que se traga los fallos por cuenta para
no romper la app), este script SÍ muestra cada error —truncados, rechazos,
fallos de red— porque durante el entrenamiento eso es justo lo que hay que ver.

Uso (desde la carpeta backend/, con el venv activo):

    python scripts/entrenar_razonador.py
    python scripts/entrenar_razonador.py "../FICHAS MAYOR.xlsx"
    python scripts/entrenar_razonador.py --motor facturas --limite 5
    python scripts/entrenar_razonador.py --repaso           # también repasa las decididas

Requiere ANTHROPIC_API_KEY en el entorno o en el .env de la raíz. Sin clave,
imprime el análisis del motor y avisa (no llama a la API).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date
from pathlib import Path

# --- Rutas: hacer importable `app` y localizar el .env de la raíz ------------
_SCRIPTS = Path(__file__).resolve().parent
_BACKEND = _SCRIPTS.parent
_RAIZ = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# La consola de Windows suele ser cp1252 y no traga los glifos de caja (═ → ✓).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _cargar_dotenv() -> None:
    """Carga el .env de la raíz sin pisar variables ya definidas (como config.py,
    pero sin los guardas de producción: esto es una herramienta de desarrollo)."""
    ruta = _RAIZ / ".env"
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


_cargar_dotenv()

from app import service                       # noqa: E402
from app.domain.models import Clasificacion   # noqa: E402
from app.domain.resultados import Informe     # noqa: E402
from app.engine import razonador              # noqa: E402


# --- Utilidades de impresión -------------------------------------------------
def _linea(c: str = "─", n: int = 78) -> str:
    return c * n


def _fecha_corte(libro) -> date | None:
    fechas = [m.fecha for m in libro.movimientos if m.fecha is not None]
    return max(fechas) if fechas else None


def _conteo_por_clasificacion(informe: Informe) -> dict[str, int]:
    conteo: dict[str, int] = {}
    for r in informe.resultados:
        conteo[r.clasificacion.value] = conteo.get(r.clasificacion.value, 0) + 1
    return conteo


def _imprimir_sugerencia(r, s: razonador.SugerenciaRazonador) -> None:
    marca = {"NO_ALARMA": "✓", "SOSPECHA_ANTIGUA": "⚠", "INCIERTO": "?"}.get(
        s.veredicto, "·"
    )
    print(f"  [{marca}] {r.codigo_cuenta}  {r.nombre_cuenta}")
    sub = f" · {s.subcategoria}" if s.subcategoria else ""
    ant = f" · {s.antiguedad_dias}d" if s.antiguedad_dias is not None else ""
    reciente = " · reciente" if s.reciente_sin_alarma else ""
    print(f"      {s.veredicto} (confianza {s.confianza}){sub}{ant}{reciente}")
    print(f"      motivo del motor: {r.motivo}")
    print(f"      → {s.motivo}")


def _imprimir_reparo(rep: razonador.RevisionRepaso) -> None:
    print(f"  [≠] {rep.codigo_cuenta}  (motor: {rep.clasificacion_motor}, "
          f"confianza {rep.confianza})")
    print(f"      → {rep.duda}")


def _profundo(informe: Informe, corte: date | None, limite: int | None) -> None:
    """Razonamiento por cuenta sobre las que el motor dejó en REVISAR."""
    revisar = [r for r in informe.resultados
               if r.clasificacion == Clasificacion.REVISAR]
    if not revisar:
        print("  (ninguna cuenta en REVISAR: el motor las resolvió todas)")
        return
    if limite is not None:
        revisar = revisar[:limite]
    print(f"  {len(revisar)} cuenta(s) en REVISAR a razonar…\n")

    ok = fallos = 0
    t0 = time.time()
    for r in revisar:
        try:
            s = razonador.razonar_cuenta(r, corte)
            _imprimir_sugerencia(r, s)
            ok += 1
        except Exception as e:  # truncado / rechazo / red: visible, no silencioso
            print(f"  [✗] {r.codigo_cuenta}  {r.nombre_cuenta}")
            print(f"      FALLO: {type(e).__name__}: {e}")
            fallos += 1
        print()
    print(f"  Profundo: {ok} ok, {fallos} fallo(s) en {time.time() - t0:.1f}s")


def _repaso(informe: Informe, corte: date | None) -> None:
    """Repaso de control de calidad sobre las cuentas ya decididas por el motor."""
    decididas = [r for r in informe.resultados if r.clasificacion in (
        Clasificacion.CONCILIADA,
        Clasificacion.SIN_FACTURA_ALTA_CONFIANZA,
        Clasificacion.FACTURA_SIN_PAGO,
    )]
    if not decididas:
        print("  (ninguna cuenta decidida que repasar)")
        return
    print(f"  Repasando {len(decididas)} cuenta(s) ya decididas…")
    try:
        reparos = razonador.repasar_cuentas(decididas, corte)
    except Exception as e:
        print(f"  FALLO del repaso: {type(e).__name__}: {e}")
        return
    if not reparos:
        print("  Sin reparos: el razonador está de acuerdo con el motor.")
        return
    print(f"  {len(reparos)} reparo(s):\n")
    for rep in reparos:
        _imprimir_reparo(rep)
        print()


def main() -> int:
    p = argparse.ArgumentParser(description="Banco de pruebas del razonador (LLM).")
    p.add_argument("fichero", nargs="?",
                   default=str(_RAIZ / "FICHAS MAYOR.xlsx"),
                   help="Libro Mayor (Excel o PDF). Por defecto: FICHAS MAYOR.xlsx")
    p.add_argument("--motor", choices=["pagos", "facturas", "both"], default="both",
                   help="Qué análisis razonar (por defecto: both).")
    p.add_argument("--limite", type=int, default=None,
                   help="Máx. de cuentas REVISAR a enviar por motor (control de coste).")
    p.add_argument("--repaso", action="store_true",
                   help="Además, repasar las cuentas ya decididas por el motor.")
    args = p.parse_args()

    ruta = Path(args.fichero)
    if not ruta.exists():
        print(f"No existe el fichero: {ruta}", file=sys.stderr)
        return 2

    print(_linea("═"))
    print(f"  Fichero: {ruta.name}")
    libro = service.parsear(ruta)
    corte = _fecha_corte(libro)
    print(f"  Apuntes: {len(libro.movimientos)} · fecha de corte: {corte}")

    informe_pagos = service.analizar_libro(libro)
    informe_facturas = service.analizar_facturas_libro(libro)
    print(f"  Pagos sin factura   → {_conteo_por_clasificacion(informe_pagos)}")
    print(f"  Facturas sin pago   → {_conteo_por_clasificacion(informe_facturas)}")
    print(_linea("═"))

    if not razonador.hay_clave():
        print("\n⚠  No hay ANTHROPIC_API_KEY en el entorno ni en el .env de la raíz.")
        print("   Añade  ANTHROPIC_API_KEY=sk-ant-...  al .env y vuelve a lanzarlo.")
        print("   (El análisis del motor de arriba sí se ha ejecutado.)")
        return 0

    print(f"\nModelo: {razonador.MODELO} · effort profundo: {razonador.EFFORT} · "
          f"repaso: {razonador.EFFORT_REPASO} · max_tokens: {razonador.MAX_TOKENS}\n")

    fuentes = []
    if args.motor in ("pagos", "both"):
        fuentes.append(("PAGOS SIN FACTURA", informe_pagos))
    if args.motor in ("facturas", "both"):
        fuentes.append(("FACTURAS SIN PAGO (análisis inverso)", informe_facturas))

    for titulo, informe in fuentes:
        print(_linea())
        print(f"  {titulo}")
        print(_linea())
        print("\n  · Razonamiento profundo (cuentas en REVISAR):\n")
        _profundo(informe, corte, args.limite)
        if args.repaso:
            print("\n  · Repaso de las cuentas ya decididas:\n")
            _repaso(informe, corte)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
