"""CDT y fondos de inversión colectiva de la SFC (datos.gov.co)."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from invest import storage
from invest.ingest import sfc_ahorro, socrata


def test_promedio_ponderado_de_cdt():
    crudos = [
        {"fechacorte": "2024-01-10T00:00:00.000", "subcuenta": "70", "tasa": "12", "monto": "100"},
        {"fechacorte": "2024-01-10T00:00:00.000", "subcuenta": "70", "tasa": "10", "monto": "300"},
        {"fechacorte": "2024-01-10T00:00:00.000", "subcuenta": "70", "tasa": "0", "monto": "500"},     # sin tasa: fuera
        {"fechacorte": "2024-01-10T00:00:00.000", "subcuenta": "130", "tasa": 11.5, "monto": 50},
        {"fechacorte": "2024-01-10T00:00:00.000", "subcuenta": "900", "tasa": "9", "monto": "100"},    # total por canal: fuera
        {"fechacorte": "2024-01-11T00:00:00.000", "subcuenta": "110", "tasa": "11", "monto": "0"},     # sin monto: fuera
    ]
    r = sfc_ahorro.promedios_cdt(crudos)
    assert [(x["fecha"], x["plazo"], x["reportes"]) for x in r] == [("2024-01-10", 90, 2), ("2024-01-10", 360, 1)]
    assert r[0]["tasa"] == pytest.approx((12 * 100 + 10 * 300) / 400)


def test_conector_cdt_guarda_series(carpeta_datos, monkeypatch):
    pedidos = []

    def falso(url, where=None, order=None, select=None, **kw):
        pedidos.append(where)
        return [{"fechacorte": "2024-01-10T00:00:00.000", "subcuenta": "70", "tasa": "10", "monto": "100"},
                {"fechacorte": "2024-01-10T00:00:00.000", "subcuenta": "130", "tasa": "11", "monto": "100"}]

    monkeypatch.setattr(socrata, "consultar", falso)
    res = sfc_ahorro.ConectorCDT().ejecutar(date(2024, 1, 1), date(2024, 1, 31))
    assert res.estado == "ok", res.detalle
    assert "uca = '1'" in pedidos[0] and "'70'" in pedidos[0]
    t = storage.leer_tabla("silver", "fact_serie_macro").sort("serie_id")
    assert t.get_column("serie_id").to_list() == ["sfc_cdt_360", "sfc_cdt_90"] and t.get_column("valor").to_list() == [11.0, 10.0]


def test_fic_solo_cierres_de_mes():
    dias = sfc_ahorro.ultimos_dias(date(2024, 1, 15), date(2024, 3, 30))
    assert dias[:4] == [date(2024, 1, 28), date(2024, 1, 29), date(2024, 1, 30), date(2024, 1, 31)]
    assert max(dias) == date(2024, 2, 29)  # marzo aún no cierra
    base = {"codigo_entidad": "5", "codigo_negocio": "100", "tipo_participacion": "800"}
    crudos = [{**base, "fecha_corte": "2024-01-30T00:00:00.000", "valor_unidad_operaciones": "10"},
              {**base, "fecha_corte": "2024-01-31T00:00:00.000", "valor_unidad_operaciones": "11"},
              {**base, "tipo_participacion": "801", "fecha_corte": "2024-01-29T00:00:00.000", "valor_unidad_operaciones": "7"},
              {**base, "tipo_participacion": "802", "fecha_corte": "2024-01-31T00:00:00.000", "valor_unidad_operaciones": "0"}]
    r = sorted(sfc_ahorro.cierre_de_mes(crudos), key=lambda x: x["tipo_participacion"])
    assert [(x["tipo_participacion"], x["valor_unidad_operaciones"]) for x in r] == [("800", "11"), ("801", "7")]


def test_conector_fic(carpeta_datos, monkeypatch):
    pedidos = []

    def falso(url, where=None, order=None, select=None, **kw):
        pedidos.append((where, select))
        return [{"fecha_corte": "2024-01-31T00:00:00.000", "codigo_entidad": "5", "nombre_entidad": "Fiduciaria X",
                 "codigo_negocio": "100", "nombre_patrimonio": "FONDO", "nombre_subtipo_patrimonio": "FIC DE TIPO GENERAL",
                 "tipo_participacion": "800", "valor_unidad_operaciones": "10500.5", "valor_fondo_cierre_dia_t": "1000000",
                 "numero_inversionistas": "12", "rentabilidad_mensual": "8.1", "rentabilidad_anual": "9.2"}]

    monkeypatch.setattr(socrata, "consultar", falso)
    res = sfc_ahorro.ConectorFIC().ejecutar(date(2024, 1, 1), date(2024, 2, 15))
    assert res.estado == "ok", res.detalle
    assert "fecha_corte in (" in pedidos[0][0] and "valor_unidad_operaciones" in pedidos[0][1]
    t = storage.leer_tabla("silver", "fact_fic_mensual")
    assert t.row(0, named=True)["valor_unidad"] == 10500.5 and t.row(0, named=True)["inversionistas"] == 12
