"""Tests del parser del fichero del banco (ficha de tesorería / extracto)."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from app.ingest.banco_parser import parsear_banco


def _xlsx(tmp_path, filas, columnas):
    ruta = tmp_path / "banco.xlsx"
    pd.DataFrame(filas, columns=columnas).to_excel(ruta, index=False)
    return ruta


def _xlsx_crudo(tmp_path, matriz):
    """Escribe una matriz tal cual (con filas de título antes de la cabecera)."""
    ruta = tmp_path / "banco.xlsx"
    pd.DataFrame(matriz).to_excel(ruta, index=False, header=False)
    return ruta


def test_ficha_mayor_banco_cabecera_desplazada_y_signo_haber_salida(tmp_path):
    """Formato real: filas de título, cabecera desplazada, Haber = salida."""
    ruta = _xlsx_crudo(tmp_path, [
        ["Fichas Mayor Pantalla", "", "", "", "", "", "", ""],
        ["NIF: B123", "", "", "", "", "", "", "Ejercicio: 2026"],
        ["", "", "", "", "", "", "", ""],
        ["Fecha asiento", "Número de asiento", "Código cuenta", "Comentario",
         "Debe", "Haber", "Saldo actual", "Contrapartida"],
        ["05/01/2026", "576", "5720002", "Pago factura", "0", "240,02", "-114,68", "4000045"],
        ["05/01/2026", "580", "5720002", "MIGUEL LARRAZ", "172,48", "0", "58,80", ""],
    ])
    ext = parsear_banco(ruta)
    assert len(ext.movimientos) == 2
    pago = ext.movimientos[0]
    assert pago.es_salida
    assert pago.importe == Decimal("-240.02")   # Haber -> salida
    assert pago.asiento == "576"
    assert pago.contrapartida == "4000045"
    assert not ext.movimientos[1].es_salida     # Debe -> entrada


def test_extracto_generico_importe_con_signo(tmp_path):
    ruta = _xlsx(tmp_path,
                 [["15/01/2026", "PAGO NATURGY", -120.50, "FRA-1"],
                  ["16/01/2026", "INGRESO", 300.00, ""]],
                 ["Fecha", "Concepto", "Importe", "Factura"])
    ext = parsear_banco(ruta)
    assert ext.movimientos[0].importe == Decimal("-120.50")
    assert ext.movimientos[0].referencia == "FRA-1"
    assert not ext.movimientos[1].es_salida


def test_cargo_abono_separados(tmp_path):
    ruta = _xlsx(tmp_path,
                 [["15/01/2026", "PAGO", 120.50, 0],
                  ["16/01/2026", "COBRO", 0, 300.00]],
                 ["Fecha", "Concepto", "Cargo", "Abono"])
    ext = parsear_banco(ruta)
    assert ext.movimientos[0].importe == Decimal("-120.50")
    assert ext.movimientos[1].importe == Decimal("300.00")


def test_formato_espanol_y_aviso_sin_asiento(tmp_path):
    ruta = _xlsx(tmp_path,
                 [["20/02/2026", "PAGO", "-1.234,56"]],
                 ["Fecha operación", "Concepto", "Importe (€)"])
    ext = parsear_banco(ruta)
    assert ext.movimientos[0].importe == Decimal("-1234.56")
    assert any("asiento" in a.lower() for a in ext.advertencias_parseo)


def test_csv_autodetecta_separador(tmp_path):
    ruta = tmp_path / "banco.csv"
    ruta.write_text("Fecha;Concepto;Importe\n15/01/2026;PAGO;-50,00\n", encoding="utf-8")
    ext = parsear_banco(ruta)
    assert ext.movimientos[0].importe == Decimal("-50.00")


def test_sin_columnas_minimas_falla(tmp_path):
    ruta = _xlsx(tmp_path, [["x", "y"]], ["Algo", "Otro"])
    with pytest.raises(ValueError, match="columnas del fichero del banco"):
        parsear_banco(ruta)
