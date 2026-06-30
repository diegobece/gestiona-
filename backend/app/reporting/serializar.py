"""Serialización del Informe a estructuras JSON-friendly (para API y exports)."""

from __future__ import annotations

from decimal import Decimal

from ..domain.models import (
    SUBCATEGORIA_INFO,
    Clasificacion,
    Movimiento,
    etiqueta_grupo,
)
from ..domain.resultados import Informe, ResultadoCuenta


def _mov(m: Movimiento) -> dict:
    return {
        "orden": m.orden,
        "fecha": m.fecha.isoformat() if m.fecha else None,
        "asiento": m.asiento,
        "tipo": m.tipo.value,
        "comentario": m.comentario,
        "debe": str(m.debe),
        "haber": str(m.haber),
        "importe_con_signo": str(m.importe_con_signo),
        "referencias": m.referencias.as_dict(),
        "saldo_reportado": str(m.saldo_reportado) if m.saldo_reportado is not None else None,
    }


def cuenta_a_dict(r: ResultadoCuenta, incluir_movimientos: bool = True) -> dict:
    d = {
        "codigo_cuenta": r.codigo_cuenta,
        "nombre_cuenta": r.nombre_cuenta,
        "clasificacion": r.clasificacion.value,
        "confianza": r.confianza.value,
        "motivo": r.motivo,
        "suma_debe": str(r.suma_debe),
        "suma_haber": str(r.suma_haber),
        "saldo_reconstruido": str(r.saldo_reconstruido),
        "saldo_reportado": str(r.saldo_reportado) if r.saldo_reportado is not None else None,
        "importe_sospechoso": str(r.importe_sospechoso),
        "num_facturas": r.num_facturas,
        "num_pagos": r.num_pagos,
        "num_abonos": r.num_abonos,
        "subcategoria": r.subcategoria,
        "subcategoria_etiqueta": SUBCATEGORIA_INFO.get(r.subcategoria, {}).get("etiqueta") if r.subcategoria else None,
        "subcategoria_accion": SUBCATEGORIA_INFO.get(r.subcategoria, {}).get("accion") if r.subcategoria else None,
        "subcategoria_motivo": r.subcategoria_motivo or None,
        "candidatos": [
            {
                "pago_orden": c.pago_orden,
                "pago_fecha": c.pago_fecha.isoformat() if c.pago_fecha else None,
                "pago_importe": str(c.pago_importe),
                "factura_cuenta": c.factura_cuenta,
                "factura_nombre": c.factura_nombre,
                "factura_fecha": c.factura_fecha.isoformat() if c.factura_fecha else None,
                "factura_importe": str(c.factura_importe),
                "factura_ref": c.factura_ref,
                "dias_desfase": c.dias_desfase,
                "confianza": c.confianza,
                "motivo": c.motivo,
                "fuente": c.fuente,
                "senales": list(c.senales),
            }
            for c in r.candidatos
        ],
        "flags": list(r.flags),
    }
    if incluir_movimientos:
        d["movimientos"] = [_mov(m) for m in r.movimientos]
    return d


def _breakdown_revisar(inf: Informe) -> list[dict]:
    """Recuento e importe de REVISAR por sub-casilla (orden de SUBCATEGORIA_INFO)."""
    acc: dict[str, dict] = {}
    for r in inf.resultados:
        if r.clasificacion != Clasificacion.REVISAR or not r.subcategoria:
            continue
        a = acc.setdefault(r.subcategoria, {"n": 0, "importe": Decimal("0.00")})
        a["n"] += 1
        a["importe"] += r.importe_sospechoso
    return [
        {
            "subcategoria": sub,
            "etiqueta": SUBCATEGORIA_INFO[sub]["etiqueta"],
            "accion": SUBCATEGORIA_INFO[sub]["accion"],
            "n": acc[sub]["n"],
            "importe": str(acc[sub]["importe"]),
        }
        for sub in SUBCATEGORIA_INFO  # orden definido (menor a mayor sospecha)
        if sub in acc
    ]


def _breakdown_fuera_alcance(inf: Informe) -> list[dict]:
    """Recuento de cuentas fuera de alcance agrupadas por grupo contable (PGC)."""
    acc: dict[str, int] = {}
    for r in inf.resultados:
        if r.clasificacion != Clasificacion.FUERA_DE_ALCANCE:
            continue
        acc[etiqueta_grupo(r.codigo_cuenta)] = acc.get(etiqueta_grupo(r.codigo_cuenta), 0) + 1
    return [{"grupo": g, "n": n} for g, n in sorted(acc.items(), key=lambda x: -x[1])]


def informe_a_dict(inf: Informe, overrides: dict | None = None,
                   ocultos: set | None = None) -> dict:
    overrides = overrides or {}
    ocultos = ocultos or set()
    cuentas = []
    for r in inf.resultados:
        d = cuenta_a_dict(r)
        ov = overrides.get(r.codigo_cuenta)
        d["override"] = (
            {"veredicto": ov.veredicto, "nota": ov.nota, "autor": ov.autor,
             "creado_en": ov.creado_en}
            if ov else None
        )
        d["mostrar"] = r.codigo_cuenta not in ocultos  # incluido en el informe PDF
        cuentas.append(d)
    return {
        "revisar_subcategorias": _breakdown_revisar(inf),
        "fuera_de_alcance": _breakdown_fuera_alcance(inf),
        "huella": inf.huella,
        "resumen": {
            "n_sin_factura": inf.resumen.n_sin_factura,
            "importe_sin_factura": str(inf.resumen.importe_sin_factura),
            "n_revisar": inf.resumen.n_revisar,
            "importe_revisar": str(inf.resumen.importe_revisar),
            "n_conciliadas": inf.resumen.n_conciliadas,
            "n_no_fiables": inf.resumen.n_no_fiables,
            "n_excluidas": inf.resumen.n_excluidas,
            "n_fuera_alcance": inf.resumen.n_fuera_alcance,
            "n_en_alcance": inf.resumen.n_en_alcance,
            "n_cuentas": inf.resumen.n_cuentas,
        },
        "flags_globales": list(inf.flags_globales),
        "advertencias_parseo": list(inf.advertencias_parseo),
        "cuentas": cuentas,
    }


def _d(s) -> Decimal:
    return Decimal(str(s))


# ===========================================================================
# Serialización del análisis INVERSO: facturas sin pago.
# ===========================================================================
from ..domain.models import (  # noqa: E402
    AGING_ORDEN,
    SUBCATEGORIA_FSP_INFO,
    incluir_en_informe_facturas,
)


def _factura(fp) -> dict:
    return {
        "fecha": fp.fecha.isoformat() if fp.fecha else None,
        "vencimiento": fp.vencimiento.isoformat() if fp.vencimiento else None,
        "importe": str(fp.importe),
        "referencia": fp.referencia,
        "nif": fp.nif,
        "antiguedad_dias": fp.antiguedad_dias,
        "vencida": fp.vencida,
        "tramo": fp.tramo,
        "comentario": fp.comentario,
    }


def _cuenta_facturas_a_dict(r: ResultadoCuenta, explicito: bool | None) -> dict:
    info = SUBCATEGORIA_FSP_INFO.get(r.subcategoria or "", {})
    return {
        "codigo_cuenta": r.codigo_cuenta,
        "nombre_cuenta": r.nombre_cuenta,
        "clasificacion": r.clasificacion.value,
        "confianza": r.confianza.value,
        "motivo": r.motivo,
        "suma_debe": str(r.suma_debe),
        "suma_haber": str(r.suma_haber),
        "importe_pendiente": str(r.importe_pendiente_pago),
        "num_facturas": r.num_facturas,
        "num_pagos": r.num_pagos,
        "subcategoria": r.subcategoria,
        "subcategoria_etiqueta": info.get("etiqueta"),
        "subcategoria_accion": info.get("accion"),
        "subcategoria_motivo": r.subcategoria_motivo or None,
        "mostrar": incluir_en_informe_facturas(r.clasificacion, explicito),
        "flags": list(r.flags),
        "facturas": [_factura(f) for f in r.facturas],
    }


def _breakdown_aging(inf: Informe) -> list[dict]:
    """Recuento e importe de facturas sin pago por tramo de antigüedad."""
    acc: dict[str, dict] = {}
    for r in inf.resultados:
        if r.clasificacion != Clasificacion.FACTURA_SIN_PAGO:
            continue
        for f in r.facturas:
            if f.importe <= 0:  # ignora abonos en el aging
                continue
            a = acc.setdefault(f.tramo, {"n": 0, "importe": Decimal("0.00")})
            a["n"] += 1
            a["importe"] += f.importe
    return [
        {"tramo": t, "n": acc[t]["n"], "importe": str(acc[t]["importe"])}
        for t in AGING_ORDEN if t in acc
    ]


def _breakdown_revisar_fsp(inf: Informe) -> list[dict]:
    """Recuento de cuentas REVISAR (infrapagadas) por razón."""
    acc: dict[str, dict] = {}
    for r in inf.resultados:
        if r.clasificacion != Clasificacion.REVISAR or not r.subcategoria:
            continue
        a = acc.setdefault(r.subcategoria, {"n": 0, "importe": Decimal("0.00")})
        a["n"] += 1
        a["importe"] += r.importe_pendiente_pago
    return [
        {"subcategoria": s, "etiqueta": SUBCATEGORIA_FSP_INFO[s]["etiqueta"],
         "accion": SUBCATEGORIA_FSP_INFO[s]["accion"], "n": acc[s]["n"],
         "importe": str(acc[s]["importe"])}
        for s in SUBCATEGORIA_FSP_INFO if s in acc
    ]


def informe_facturas_a_dict(inf: Informe, visibilidad: dict | None = None) -> dict:
    vis = visibilidad or {}
    cuentas = [_cuenta_facturas_a_dict(r, vis.get(r.codigo_cuenta))
               for r in inf.resultados]
    return {
        "modo": "facturas_sin_pago",
        "aging": _breakdown_aging(inf),
        "revisar_subcategorias": _breakdown_revisar_fsp(inf),
        "huella": inf.huella,
        "resumen": {
            "n_facturas_sin_pago": inf.resumen.n_facturas_sin_pago,
            "importe_facturas_sin_pago": str(inf.resumen.importe_facturas_sin_pago),
            "n_revisar": inf.resumen.n_revisar,
            "importe_revisar": str(inf.resumen.importe_revisar),
            "importe_pendiente_total": str(inf.resumen.importe_pendiente_total),
            "n_conciliadas": inf.resumen.n_conciliadas,
            "n_no_fiables": inf.resumen.n_no_fiables,
            "n_excluidas": inf.resumen.n_excluidas,
            "n_fuera_alcance": inf.resumen.n_fuera_alcance,
            "n_en_alcance": inf.resumen.n_en_alcance,
            "n_cuentas": inf.resumen.n_cuentas,
        },
        "flags_globales": list(inf.flags_globales),
        "advertencias_parseo": list(inf.advertencias_parseo),
        "cuentas": cuentas,
    }
