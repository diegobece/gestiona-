"""Ingesta multiformato (estructurado) -> modelo canónico.

Fase 1: CSV · Excel (XLSX) · JSON, con mapeo de columnas configurable (los exports
de Movie Magic varían). PDF/escaneado (OCR+LLM) queda para una fase posterior.
Todos los formatos convergen en el esquema canónico, así el resto es agnóstico.

Idempotencia: los IDs se derivan del contenido (proyecto+código / proyecto+nº doc),
de modo que reimportar la misma carpeta no duplica registros.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from .models import (
    CERO,
    Factura,
    LineaDoc,
    LineaPresupuesto,
    Nivel,
    OrdenCompra,
    Proveedor,
)

_RUIDO_PROV = frozenset({"sl", "slu", "sa", "sau", "lda", "ltd", "inc", "bv",
                         "gmbh", "srl", "sc", "cb", "the", "and"})

# Sinónimos de cabecera por campo canónico (auto-detección; se puede sobreescribir).
_SINONIMOS = {
    "code": ["code", "codigo", "código", "cuenta", "account", "acct", "nº cuenta"],
    "parent_code": ["parent", "parent_code", "padre", "cuenta_padre"],
    "level": ["level", "nivel", "tipo"],
    "department": ["department", "departamento", "depto", "dept"],
    "description": ["description", "descripcion", "descripción", "concepto", "detalle"],
    "budget_amount": ["budget", "presupuesto", "amount", "importe", "total", "subtotal"],
    "is_fringe": ["fringe", "fringes", "carga", "cargas_sociales"],
    "pgc": ["pgc", "cuenta_pgc", "cuenta contable"],
    "po_number": ["po", "po_number", "orden", "nº po", "purchase_order", "pedido"],
    "invoice_number": ["invoice", "factura", "nº factura", "invoice_number", "num_factura"],
    "vendor": ["vendor", "proveedor", "supplier", "tercero", "nombre"],
    "cif": ["cif", "nif", "tax_id", "vat", "cif/nif"],
    "date": ["date", "fecha", "fecha_emision", "fecha emisión"],
    "service_date": ["service_date", "fecha_devengo", "devengo", "fecha_servicio"],
    "net": ["net", "neto", "base", "base_imponible", "subtotal"],
    "tax": ["tax", "iva", "cuota", "cuota_iva"],
    "tax_rate": ["tax_rate", "tipo_iva", "%iva", "porcentaje_iva"],
    "irpf": ["irpf", "retencion", "retención"],
    "total": ["total", "importe_total", "total_factura", "gross", "bruto"],
    "currency": ["currency", "moneda", "divisa"],
    "account_code": ["account", "cuenta", "code", "codigo", "linea", "budget_line"],
}


# --------------------------------------------------------------------- helpers
def _norm_cab(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _to_decimal(v) -> Decimal:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return CERO
    if isinstance(v, str):
        v = v.strip().replace("€", "").replace(" ", "")
        if not v:
            return CERO
        if "," in v and "." in v:           # 1.234,56 -> 1234.56
            v = v.replace(".", "").replace(",", ".")
        elif "," in v:                       # 1234,56 -> 1234.56
            v = v.replace(",", ".")
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return CERO


def _to_date(v) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        s = str(v).strip()
        # ISO (YYYY-MM-DD) sin dayfirst; europeo (DD/MM/YYYY) con dayfirst.
        iso = bool(re.match(r"^\d{4}-\d{1,2}-\d{1,2}", s))
        ts = pd.to_datetime(v, errors="coerce", dayfirst=not iso)
        return None if pd.isna(ts) else ts.date()
    except (ValueError, TypeError):
        return None


def _to_str(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if isinstance(v, float) and v.is_integer():
        s = str(int(v))
    return s or None


def normalizar_proveedor(nombre: str) -> str:
    s = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode()
    toks = [t for t in re.findall(r"[a-z0-9]{2,}", s.lower()) if t not in _RUIDO_PROV]
    return " ".join(toks)


def _booly(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "si", "sí", "x", "yes", "y")


# ------------------------------------------------------------- lectura tabular
def _leer_tabla(ruta: str | Path) -> list[dict]:
    ext = Path(ruta).suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(ruta, dtype=object)
    elif ext == ".csv":
        df = pd.read_csv(ruta, dtype=object, sep=None, engine="python")
    elif ext == ".json":
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        df = pd.DataFrame(datos if isinstance(datos, list) else datos.get("items", []))
    else:
        raise ValueError(f"Formato no soportado en Fase 1: {ext} (usa CSV/Excel/JSON)")
    df.columns = [_norm_cab(c) for c in df.columns]
    return df.to_dict("records")


def detectar_mapeo(columnas: list[str], campos: list[str]) -> dict[str, str]:
    """Auto-detecta {campo_canónico: columna_origen} a partir de sinónimos.

    Casa por igualdad exacta o porque el sinónimo sea un token de la columna
    (`budget_amount` ⇢ sinónimo `budget`). Greedy y sin reutilizar columnas."""
    cols = [_norm_cab(c) for c in columnas]
    tokens = {c: set(c.split("_")) for c in cols}
    mapeo, usadas = {}, set()
    for campo in campos:
        syns = [_norm_cab(s) for s in _SINONIMOS.get(campo, [campo])]
        elegido = None
        for syn in syns:                       # 1) igualdad exacta
            for c in cols:
                if c not in usadas and c == syn:
                    elegido = c
                    break
            if elegido:
                break
        if not elegido:                         # 2) token dentro de la columna
            for syn in syns:
                for c in cols:
                    if c not in usadas and syn in tokens[c]:
                        elegido = c
                        break
                if elegido:
                    break
        if elegido:
            mapeo[campo] = elegido
            usadas.add(elegido)
    return mapeo


def _val(fila: dict, mapeo: dict, campo: str):
    col = mapeo.get(campo)
    return fila.get(col) if col else None


# ---------------------------------------------------------------- presupuesto
def parsear_presupuesto(ruta, project_id: str, mapeo: dict | None = None,
                        tipo_iva_defecto: Decimal = Decimal("0.21")
                        ) -> list[LineaPresupuesto]:
    filas = _leer_tabla(ruta)
    cols = list(filas[0].keys()) if filas else []
    m = mapeo or detectar_mapeo(cols, ["code", "parent_code", "level", "department",
                                       "description", "budget_amount", "is_fringe", "pgc"])
    if "code" not in m or "budget_amount" not in m:
        raise ValueError("El presupuesto necesita al menos columnas de código e importe "
                         f"(detectado: {sorted(m)}). Indica el mapeo manualmente.")
    out = []
    for fila in filas:
        code = _to_str(_val(fila, m, "code"))
        if not code:
            continue
        neto = _to_decimal(_val(fila, m, "budget_amount"))
        nivel_raw = (_to_str(_val(fila, m, "level")) or "").lower()
        nivel = Nivel(nivel_raw) if nivel_raw in (n.value for n in Nivel) else Nivel.DETAIL
        out.append(LineaPresupuesto(
            id=f"{project_id}:{code}", project_id=project_id, code=code,
            description=_to_str(_val(fila, m, "description")) or code,
            budget_amount=neto,
            budget_amount_bruto=(neto * (Decimal("1") + tipo_iva_defecto)).quantize(Decimal("0.01")),
            nivel=nivel, parent_code=_to_str(_val(fila, m, "parent_code")),
            department=_to_str(_val(fila, m, "department")),
            is_fringe=_booly(_val(fila, m, "is_fringe")),
            pgc_cuenta=_to_str(_val(fila, m, "pgc")),
        ))
    return out


# ----------------------------------------------------- proveedores (registro)
class RegistroProveedores:
    """Resuelve vendor_id por CIF y, en su defecto, por nombre normalizado."""

    def __init__(self) -> None:
        self._por_cif: dict[str, Proveedor] = {}
        self._por_norm: dict[str, Proveedor] = {}

    def resolver(self, nombre: str | None, cif: str | None) -> Proveedor | None:
        nombre = (nombre or "").strip()
        cif = (cif or "").strip().upper() or None
        if not nombre and not cif:
            return None
        norm = normalizar_proveedor(nombre)
        if cif and cif in self._por_cif:
            return self._por_cif[cif]
        if norm and norm in self._por_norm:
            return self._por_norm[norm]
        pid = cif or norm or nombre
        prov = Proveedor(id=f"v:{pid}", nombre=nombre or cif, nombre_norm=norm, cif=cif)
        if cif:
            self._por_cif[cif] = prov
        if norm:
            self._por_norm[norm] = prov
        return prov

    def todos(self) -> list[Proveedor]:
        vistos, out = set(), []
        for p in list(self._por_cif.values()) + list(self._por_norm.values()):
            if p.id not in vistos:
                vistos.add(p.id)
                out.append(p)
        return out


# ------------------------------------------------------------------------ PO
def parsear_pos(ruta, project_id: str, registro: RegistroProveedores,
                mapeo: dict | None = None) -> list[OrdenCompra]:
    filas = _leer_tabla(ruta)
    cols = list(filas[0].keys()) if filas else []
    m = mapeo or detectar_mapeo(cols, ["po_number", "vendor", "cif", "date", "net",
                                       "tax", "total", "currency", "account_code", "description"])
    out = []
    for fila in filas:
        po_number = _to_str(_val(fila, m, "po_number"))
        if not po_number:
            continue
        prov = registro.resolver(_to_str(_val(fila, m, "vendor")), _to_str(_val(fila, m, "cif")))
        net, tax, total = _importes(fila, m)
        acc = _to_str(_val(fila, m, "account_code"))
        out.append(OrdenCompra(
            id=f"{project_id}:po:{po_number}", project_id=project_id, po_number=po_number,
            proveedor_id=prov.id if prov else None, fecha=_to_date(_val(fila, m, "date")),
            net=net, tax=tax, total=total,
            moneda=_to_str(_val(fila, m, "currency")) or "EUR",
            budget_line_id=f"{project_id}:{acc}" if acc else None,
            source_file=str(Path(ruta).name), source_format=Path(ruta).suffix.lstrip("."),
            raw={k: _to_str(v) for k, v in fila.items()},
        ))
    return out


# ------------------------------------------------------------------- factura
def parsear_facturas(ruta, project_id: str, registro: RegistroProveedores,
                     mapeo: dict | None = None) -> list[Factura]:
    filas = _leer_tabla(ruta)
    cols = list(filas[0].keys()) if filas else []
    m = mapeo or detectar_mapeo(cols, ["invoice_number", "vendor", "cif", "date",
                                       "service_date", "net", "tax", "tax_rate", "irpf",
                                       "total", "currency", "po_number", "account_code"])
    out = []
    for fila in filas:
        num = _to_str(_val(fila, m, "invoice_number"))
        if not num:
            continue
        prov = registro.resolver(_to_str(_val(fila, m, "vendor")), _to_str(_val(fila, m, "cif")))
        net, tax, total = _importes(fila, m)
        acc = _to_str(_val(fila, m, "account_code"))
        po_number = _to_str(_val(fila, m, "po_number"))
        es_abono = total < 0 or net < 0
        out.append(Factura(
            id=f"{project_id}:inv:{(prov.id if prov else 'na')}:{num}", project_id=project_id,
            invoice_number=num, proveedor_id=prov.id if prov else None,
            fecha=_to_date(_val(fila, m, "date")),
            service_date=_to_date(_val(fila, m, "service_date")),
            net=net, tax=tax, total=total,
            tax_rate=_to_decimal(_val(fila, m, "tax_rate")) or None,
            irpf=_to_decimal(_val(fila, m, "irpf")),
            moneda=_to_str(_val(fila, m, "currency")) or "EUR",
            po_id=f"{project_id}:po:{po_number}" if po_number else None,
            budget_line_id=f"{project_id}:{acc}" if acc else None,
            es_abono=es_abono,
            source_file=str(Path(ruta).name), source_format=Path(ruta).suffix.lstrip("."),
            raw={k: _to_str(v) for k, v in fila.items()},
        ))
    return out


def _importes(fila: dict, m: dict) -> tuple[Decimal, Decimal, Decimal]:
    """Reconstruye neto/IVA/total de forma robusta aunque falte alguno."""
    net = _to_decimal(_val(fila, m, "net"))
    tax = _to_decimal(_val(fila, m, "tax"))
    total = _to_decimal(_val(fila, m, "total"))
    if total == CERO and (net or tax):
        total = (net + tax).quantize(Decimal("0.01"))
    if net == CERO and total:
        net = (total - tax).quantize(Decimal("0.01"))
    return net, tax, total
