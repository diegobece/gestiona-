"""Smoke tests de service, persistencia y exportación."""

from __future__ import annotations

from pathlib import Path

from app.persistence.store import OverrideStore
from app.reporting.excel_export import exportar_excel
from app.reporting.pdf_export import exportar_pdf
from app.reporting.serializar import informe_a_dict
from app.service import analizar_libro
from tests.factories import factura, libro, pago


def _informe():
    return analizar_libro(libro(
        pago("4000500", 100, saldo=100),
        factura("4100061", 50), pago("4100061", 80),
        factura("4000003", 200),
    ))


def test_serializa_a_dict_con_evidencia():
    d = informe_a_dict(_informe())
    assert "resumen" in d and "cuentas" in d and d["huella"]
    for c in d["cuentas"]:
        assert "motivo" in c and c["motivo"]            # nunca etiqueta sin motivo
        assert "movimientos" in c                       # evidencia presente


def test_export_excel_genera_bytes():
    data = exportar_excel(_informe())
    assert data[:2] == b"PK" and len(data) > 1000       # zip/xlsx válido


def test_export_pdf_genera_bytes():
    data = exportar_pdf(_informe())
    assert data[:4] == b"%PDF" and len(data) > 500


def test_pdf_respeta_cuentas_ocultas():
    inf = analizar_libro(libro(
        pago("4000500", 100, saldo=100),   # SIN_FACTURA
        pago("4000600", 200, saldo=200),   # SIN_FACTURA
    ))
    completo = exportar_pdf(inf)
    sin_una = exportar_pdf(inf, ocultos={"4000600"})
    sin_ambas = exportar_pdf(inf, ocultos={"4000500", "4000600"})
    # Ocultar cuentas reduce el contenido del informe.
    assert len(sin_una) < len(completo)
    assert len(sin_ambas) < len(sin_una)
    assert sin_ambas[:4] == b"%PDF"   # sigue siendo un PDF válido (vacío de cuentas)


def test_visibilidad_persiste(tmp_path: Path):
    store = OverrideStore(tmp_path / "ov.db")
    assert store.ocultos("H1") == set()
    store.set_visibilidad("H1", "4000600", mostrar=False)
    store.set_visibilidad("H1", "4000500", mostrar=True)
    assert store.ocultos("H1") == {"4000600"}
    # Reactivar la quita de ocultos (idempotente).
    store.set_visibilidad("H1", "4000600", mostrar=True)
    assert store.ocultos("H1") == set()


def test_overrides_persisten_y_se_recuperan(tmp_path: Path):
    store = OverrideStore(tmp_path / "ov.db")
    store.guardar("HUELLA1", "4000500", "EN_OTRA_CUENTA", nota="está en 410", autor="ana")
    recuperado = store.listar("HUELLA1")
    assert recuperado["4000500"].veredicto == "EN_OTRA_CUENTA"
    # idempotente: re-guardar actualiza, no duplica
    store.guardar("HUELLA1", "4000500", "SIN_FACTURA")
    assert store.listar("HUELLA1")["4000500"].veredicto == "SIN_FACTURA"
