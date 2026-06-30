"""Exportación del informe a PDF — informe profesional para el cliente.

Diseño: cabecera de marca (logo/wordmark Gestiona más + color corporativo) y, a
diferencia del resto de la app, lista SOLO los **pagos sin factura claros**
(`SIN_FACTURA_ALTA_CONFIANZA`). Las cuentas en REVISAR NO se incluyen: caen ahí
por otros motivos (arrastre, desfase, factura en otra cuenta…) y no son pagos sin
factura claros. Cada pago se muestra con su fecha, asiento, concepto e importe.

Si existe `static/logo.png`, se usa como logo; si no, se dibuja el wordmark.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF

from ..domain.models import (
    AGING_ORDEN,
    Clasificacion,
    TipoMovimiento,
    incluir_en_informe_facturas,
)
from ..domain.resultados import Informe

# --- Marca ------------------------------------------------------------------
BRAND = (196, 30, 45)        # rojo corporativo Gestiona más
BRAND_DARK = (150, 22, 34)
TEXT = (45, 45, 50)
GREY = (120, 120, 128)
LINE = (220, 220, 226)
ZEBRA = (247, 247, 249)

_FONTS = Path("C:/Windows/Fonts")
_STATIC = Path(__file__).parent.parent / "static"
_LOGO = next((p for n in ("logo.png", "logo.jpg", "logo.jpeg")
              if (p := _STATIC / n).exists()), None)

MARGIN = 16


def _eur(v) -> str:
    s = f"{Decimal(str(v)):,.2f}"
    s = s.replace(",", "·").replace(".", ",").replace("·", ".")
    return f"{s} €"


def _fecha(d) -> str:
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return d
    return d.strftime("%d/%m/%Y") if d else "—"


class _Informe(FPDF):
    _AVISO_PAGOS = (
        "Este informe relaciona pagos para los que no consta factura en su "
        "propia cuenta; la factura podría estar en otra cuenta o ejercicio. "
        "Requiere verificación. Documento confidencial — Gestiona más.")
    _AVISO_FACTURAS = (
        "Este informe relaciona facturas sin ningún pago registrado en su cuenta. "
        "Puede tratarse de deuda viva normal o de pagos pendientes; revíselo. "
        "Documento confidencial — Gestiona más.")

    def __init__(self, subtitulo: str = "INFORME DE PAGOS SIN FACTURA",
                 aviso: str | None = None):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.subtitulo = subtitulo
        self.aviso = aviso or self._AVISO_PAGOS
        self.set_auto_page_break(auto=True, margin=22)
        self.set_margins(MARGIN, 16, MARGIN)
        self._fuentes_ok = self._cargar_fuentes()

    def _cargar_fuentes(self) -> bool:
        try:
            self.add_font("body", "", str(_FONTS / "segoeui.ttf"))
            self.add_font("body", "B", str(_FONTS / "segoeuib.ttf"))
            self.add_font("brand", "", str(_FONTS / "georgiab.ttf"))
            self.add_font("script", "", str(_FONTS / "segoesc.ttf"))
            return True
        except Exception:
            return False

    def f(self, estilo: str = "", size: float = 10):
        """Selecciona fuente con fallback a core si no hay TTF."""
        if self._fuentes_ok:
            self.set_font("body", "B" if estilo == "B" else "", size)
        else:
            self.set_font("Helvetica", "B" if estilo == "B" else "", size)

    # -------------------------------------------------------------- cabecera
    def header(self):
        if _LOGO is not None:
            self.image(str(_LOGO), x=MARGIN, y=10, h=14)
        else:
            self._wordmark(MARGIN, 11)
        # Etiqueta a la derecha
        self.set_xy(-90 - MARGIN, 12)
        self.set_text_color(*GREY)
        self.f("", 8)
        self.cell(90, 4, self.subtitulo, align="R",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_x(-90 - MARGIN)
        self.cell(90, 4, f"Generado el {datetime.now().strftime('%d/%m/%Y')}",
                  align="R")
        # Regla roja
        self.set_draw_color(*BRAND)
        self.set_line_width(0.6)
        self.line(MARGIN, 27, self.w - MARGIN, 27)
        self.set_y(34)

    def _wordmark(self, x, y):
        if self._fuentes_ok:
            self.set_text_color(*BRAND)
            self.set_font("brand", "", 21)
            self.set_xy(x, y)
            self.cell(self.get_string_width("Gestiona") + 2, 9, "Gestiona")
            self.set_font("script", "", 26)
            self.set_xy(x + self.get_string_width("Gestiona") - 30, y + 4.5)
            self.set_font("script", "", 26)
            self.cell(30, 9, "más")
        else:
            self.set_text_color(*BRAND)
            self.set_font("Helvetica", "B", 22)
            self.set_xy(x, y)
            self.cell(60, 9, "Gestiona mas")

    # ---------------------------------------------------------------- pie
    def footer(self):
        self.set_y(-18)
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
        self.set_text_color(*GREY)
        self.f("", 7)
        self.set_xy(MARGIN, -15)
        self.multi_cell(self.w - 2 * MARGIN - 30, 3.4, self.aviso, align="L")
        self.set_xy(self.w - MARGIN - 28, -15)
        self.cell(28, 3.4, f"Página {self.page_no()} de {{nb}}", align="R")


def exportar_pdf(inf: Informe, ocultos: set[str] | None = None,
                 visibilidad: dict[str, bool] | None = None) -> bytes:
    ocultos = ocultos or set()
    visibilidad = visibilidad or {}
    pdf = _Informe()
    pdf.add_page()

    cuentas = [
        r for r in inf.resultados
        if (r.clasificacion == Clasificacion.SIN_FACTURA_ALTA_CONFIANZA
            and r.codigo_cuenta not in ocultos)
        or (r.clasificacion == Clasificacion.REVISAR
            and visibilidad.get(r.codigo_cuenta) is True)
    ]
    total = sum((r.importe_sospechoso for r in cuentas), Decimal("0.00"))

    # --- Título e introducción ---------------------------------------------
    pdf.set_text_color(*TEXT)
    pdf.f("B", 22)
    pdf.cell(0, 11, "Pagos sin factura", new_x="LMARGIN", new_y="NEXT")
    pdf.f("", 10)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(
        0, 5,
        "Relación de pagos registrados en cuentas de proveedor y acreedor para los "
        "que no se ha localizado una factura que los respalde en la propia cuenta. "
        "Se excluyen las partidas en revisión por otros motivos.",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(3)

    # --- Banda resumen ------------------------------------------------------
    y = pdf.get_y()
    pdf.set_fill_color(*BRAND)
    pdf.rect(MARGIN, y, pdf.w - 2 * MARGIN, 16, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(MARGIN + 5, y + 3)
    pdf.f("", 9)
    pdf.cell(0, 5, "PAGOS SIN FACTURA DETECTADOS", new_x="LMARGIN", new_y="NEXT")
    n_pagos = sum(r.num_pagos for r in cuentas)
    pdf.set_xy(MARGIN + 5, y + 8)
    pdf.f("B", 14)
    pdf.cell(90, 7, f"{n_pagos} pago(s) en {len(cuentas)} cuenta(s)")
    pdf.set_xy(pdf.w - MARGIN - 75, y + 4)
    pdf.f("B", 16)
    pdf.cell(70, 8, f"Total: {_eur(total)}", align="R")
    pdf.set_y(y + 16)
    pdf.ln(6)

    if not cuentas:
        pdf.set_text_color(*TEXT)
        pdf.f("", 11)
        pdf.multi_cell(0, 6, "No se han detectado pagos sin factura claros en este "
                             "Libro Mayor.", new_x="LMARGIN", new_y="NEXT")
    for r in cuentas:
        _seccion_cuenta(pdf, r)

    return bytes(pdf.output())


# Columnas de la tabla de pagos (mm).
_COLS = [("Fecha", 26), ("Asiento", 24), ("Concepto", 88), ("Importe", 40)]


def _seccion_cuenta(pdf: _Informe, r) -> None:
    pagos = [m for m in r.movimientos if m.tipo == TipoMovimiento.PAGO]
    nif = next((m.referencias.nif for m in r.movimientos if m.referencias.nif), None)

    # Evita cortar la cabecera de la cuenta al final de página.
    if pdf.get_y() > pdf.h - 55:
        pdf.add_page()

    # Cabecera de cuenta
    pdf.set_draw_color(*BRAND)
    pdf.set_fill_color(*BRAND)
    pdf.rect(MARGIN, pdf.get_y(), 3, 7, style="F")
    pdf.set_xy(MARGIN + 5, pdf.get_y())
    pdf.set_text_color(*TEXT)
    pdf.f("B", 12)
    pdf.cell(110, 7, f"{r.codigo_cuenta}  ·  {r.nombre_cuenta}")
    pdf.f("B", 12)
    pdf.set_text_color(*BRAND)
    pdf.cell(0, 7, _eur(r.importe_sospechoso), align="R", new_x="LMARGIN", new_y="NEXT")
    if nif:
        pdf.set_x(MARGIN + 5)
        pdf.set_text_color(*GREY)
        pdf.f("", 8)
        pdf.cell(0, 4, f"NIF/CIF: {nif}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)

    # Cabecera de tabla
    pdf.set_x(MARGIN)
    pdf.set_fill_color(238, 238, 241)
    pdf.set_text_color(*GREY)
    pdf.f("B", 8.5)
    for i, (titulo, w) in enumerate(_COLS):
        align = "R" if titulo == "Importe" else "L"
        pdf.cell(w, 6.5, titulo.upper(), border=0, fill=True, align=align)
    pdf.ln(6.5)

    # Filas
    pdf.f("", 9.5)
    for j, m in enumerate(pagos):
        if pdf.get_y() > pdf.h - 26:
            pdf.add_page()
        fill = j % 2 == 1
        if fill:
            pdf.set_fill_color(*ZEBRA)
        pdf.set_x(MARGIN)
        pdf.set_text_color(*TEXT)
        concepto = (m.comentario or "").strip()
        if len(concepto) > 60:
            concepto = concepto[:57] + "…"
        pdf.cell(_COLS[0][1], 6.5, _fecha(m.fecha), border=0, fill=fill)
        pdf.cell(_COLS[1][1], 6.5, str(m.asiento or "—"), border=0, fill=fill)
        pdf.cell(_COLS[2][1], 6.5, concepto, border=0, fill=fill)
        pdf.f("B", 9.5)
        pdf.cell(_COLS[3][1], 6.5, _eur(m.debe), border=0, fill=fill, align="R")
        pdf.f("", 9.5)
        pdf.ln(6.5)
    # Línea inferior de la tabla
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    pdf.line(MARGIN, pdf.get_y(), pdf.w - MARGIN, pdf.get_y())
    pdf.ln(7)


# ===========================================================================
# Informe inverso: FACTURAS SIN PAGO (cliente)
# ===========================================================================
_COLS_F = [("Fecha", 24), ("Vencim.", 24), ("Antigüedad", 28),
           ("Referencia", 62), ("Importe", 40)]


def _antiguedad_txt(f) -> str:
    if f.antiguedad_dias is None:
        return "—"
    if f.vencida:
        return f"vencida {f.antiguedad_dias} d"
    return f"{f.antiguedad_dias} d"


def exportar_pdf_facturas(inf: Informe, visibilidad: dict | None = None) -> bytes:
    vis = visibilidad or {}
    pdf = _Informe(subtitulo="INFORME DE FACTURAS SIN PAGO",
                   aviso=_Informe._AVISO_FACTURAS)
    pdf.add_page()

    # FACTURA_SIN_PAGO (por defecto) + REVISAR añadidas explícitamente al revisarlas.
    cuentas = [r for r in inf.resultados
               if incluir_en_informe_facturas(r.clasificacion, vis.get(r.codigo_cuenta))]
    total = sum((r.importe_pendiente_pago for r in cuentas), Decimal("0.00"))
    n_fact = sum(len([f for f in r.facturas if f.importe > 0]) for r in cuentas)

    pdf.set_text_color(*TEXT)
    pdf.f("B", 22)
    pdf.cell(0, 11, "Facturas sin pago", new_x="LMARGIN", new_y="NEXT")
    pdf.f("", 10)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(
        0, 5,
        "Relación de facturas registradas en cuentas de proveedor y acreedor sin "
        "ningún pago en su propia cuenta, con su antigüedad. Puede tratarse de deuda "
        "pendiente normal; revise especialmente las de mayor antigüedad.",
        new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Banda resumen
    y = pdf.get_y()
    pdf.set_fill_color(*BRAND)
    pdf.rect(MARGIN, y, pdf.w - 2 * MARGIN, 16, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(MARGIN + 5, y + 3)
    pdf.f("", 9)
    pdf.cell(0, 5, "FACTURAS SIN PAGO", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(MARGIN + 5, y + 8)
    pdf.f("B", 14)
    pdf.cell(95, 7, f"{n_fact} factura(s) en {len(cuentas)} cuenta(s)")
    pdf.set_xy(pdf.w - MARGIN - 75, y + 4)
    pdf.f("B", 16)
    pdf.cell(70, 8, f"Pendiente: {_eur(total)}", align="R")
    pdf.set_y(y + 16)
    pdf.ln(6)

    if not cuentas:
        pdf.set_text_color(*TEXT)
        pdf.f("", 11)
        pdf.multi_cell(0, 6, "No se han detectado facturas sin pago claras (cuentas "
                             "con facturas y cero pagos) en este Libro Mayor.",
                       new_x="LMARGIN", new_y="NEXT")
    for r in cuentas:
        _seccion_cuenta_facturas(pdf, r)

    return bytes(pdf.output())


def _seccion_cuenta_facturas(pdf: _Informe, r) -> None:
    facturas = [f for f in r.facturas if f.importe > 0]
    nif = next((f.nif for f in r.facturas if f.nif), None)
    if pdf.get_y() > pdf.h - 55:
        pdf.add_page()

    pdf.set_fill_color(*BRAND)
    pdf.rect(MARGIN, pdf.get_y(), 3, 7, style="F")
    pdf.set_xy(MARGIN + 5, pdf.get_y())
    pdf.set_text_color(*TEXT)
    pdf.f("B", 12)
    pdf.cell(110, 7, f"{r.codigo_cuenta}  ·  {r.nombre_cuenta}")
    pdf.set_text_color(*BRAND)
    pdf.cell(0, 7, _eur(r.importe_pendiente_pago), align="R",
             new_x="LMARGIN", new_y="NEXT")
    if nif:
        pdf.set_x(MARGIN + 5)
        pdf.set_text_color(*GREY)
        pdf.f("", 8)
        pdf.cell(0, 4, f"NIF/CIF: {nif}", new_x="LMARGIN", new_y="NEXT")
    if r.clasificacion == Clasificacion.REVISAR:
        pdf.set_x(MARGIN + 5)
        pdf.set_text_color(*BRAND_DARK)
        pdf.f("", 8)
        pdf.cell(0, 4, f"Pago parcial — pendiente neto {_eur(r.importe_pendiente_pago)} "
                       f"(se listan todas las facturas de la cuenta)",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)

    pdf.set_x(MARGIN)
    pdf.set_fill_color(238, 238, 241)
    pdf.set_text_color(*GREY)
    pdf.f("B", 8.5)
    for titulo, w in _COLS_F:
        pdf.cell(w, 6.5, titulo.upper(), border=0, fill=True,
                 align="R" if titulo == "Importe" else "L")
    pdf.ln(6.5)

    pdf.f("", 9.5)
    for j, f in enumerate(facturas):
        if pdf.get_y() > pdf.h - 26:
            pdf.add_page()
        fill = j % 2 == 1
        if fill:
            pdf.set_fill_color(*ZEBRA)
        pdf.set_x(MARGIN)
        pdf.set_text_color(*TEXT)
        ref = (f.referencia or "—")[:42]
        pdf.cell(_COLS_F[0][1], 6.5, _fecha(f.fecha), border=0, fill=fill)
        pdf.cell(_COLS_F[1][1], 6.5, _fecha(f.vencimiento) if f.vencimiento else "—",
                 border=0, fill=fill)
        if f.vencida:
            pdf.set_text_color(*BRAND)
        pdf.cell(_COLS_F[2][1], 6.5, _antiguedad_txt(f), border=0, fill=fill)
        pdf.set_text_color(*TEXT)
        pdf.cell(_COLS_F[3][1], 6.5, ref, border=0, fill=fill)
        pdf.f("B", 9.5)
        pdf.cell(_COLS_F[4][1], 6.5, _eur(f.importe), border=0, fill=fill, align="R")
        pdf.f("", 9.5)
        pdf.ln(6.5)
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    pdf.line(MARGIN, pdf.get_y(), pdf.w - MARGIN, pdf.get_y())
    pdf.ln(7)
