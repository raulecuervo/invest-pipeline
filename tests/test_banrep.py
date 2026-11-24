from datetime import date

import polars as pl

from invest import storage
from invest.ingest import banrep

# Estructura real de la respuesta de SUAMECA (recortada): milisegundos UTC a la medianoche de Bogotá
RESPUESTA = [
    {"id": 241, "nombre": "Indicador Bancario de Referencia (IBR) overnight, nominal", "unidadCorta": "%",
     "descripcionPeriodicidad": "Diaria", "data": [[1199250000000, 8.950], [1199336400000, 8.970], [1795410000000, 11.411]]},
    {"id": 242, "nombre": "Indicador Bancario de Referencia (IBR) a 1 mes, nominal", "unidadCorta": "%",
     "descripcionPeriodicidad": "Diaria", "data": [[1199250000000, None], [1795410000000, 11.5]]},
]


def test_fechas_de_bogota():
    assert banrep.fecha_bogota(1199250000000) == date(2008, 1, 2)
    assert banrep.fecha_bogota(1795410000000) == date(2026, 11, 23)


def test_conector(carpeta_datos, monkeypatch):
    urls = []

    def leer(url):
        urls.append(url)
        return RESPUESTA if "241" in url else [{"id": 59, "nombre": "Tasa de política monetaria", "unidadCorta": "%",
                                                "descripcionPeriodicidad": "Diaria", "data": [[1795410000000, 12.25]]}]

    monkeypatch.setattr(banrep, "leer_series", leer)
    res = banrep.ConectorBanRep().ejecutar(date(2000, 1, 1), date(2026, 11, 23))
    assert res.estado == "ok", res.detalle
    assert len(urls) == 2 and "banrep_241 = Indicador Bancario" in res.detalle
    df = storage.leer_tabla("silver", "fact_serie_macro")
    assert df.filter(pl.col("serie_id") == "banrep_241").height == 3
    assert df.filter(pl.col("serie_id") == "banrep_242").height == 1  # el punto sin valor se omite
    assert df.filter((pl.col("serie_id") == "banrep_59") & (pl.col("fecha") == date(2026, 11, 23))).item(0, "valor") == 12.25


def test_dos_formas_de_respuesta():
    unica = {"SERIES": [{"id": 59, "data": [[1795410000000, 12.25]]}]}
    menu_sin_datos = {"SERIES": [{"id": 241, "data": []}]}
    assert [s["id"] for s in banrep.series_en(unica)] == [59]
    assert banrep.series_en(menu_sin_datos) == []
    assert [s["id"] for s in banrep.series_en(RESPUESTA)] == [241, 242]
