from datetime import date

import polars as pl

from invest import sucesiones

ESQ = {"fecha": pl.Date, "especie": pl.Utf8, "codigo_titulo": pl.Utf8, "precio_promedio": pl.Float64,
       "valor_transado": pl.Float64, "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC")}
LISTA = [{"especie": "cotk7paao003", "antecesora": {"nemotecnico": "BCOLOMBIA", "especie_sfc": "cob07paao000"},
          "ultimo_dia": date(2025, 5, 16), "acciones_por_accion": 1, "respaldo_csv": "BCOLOMBIA.csv"}]


def fila(f, especie, precio):
    return {"fecha": f, "especie": especie, "codigo_titulo": especie, "precio_promedio": precio, "valor_transado": 1e9,
            "fuente_id": "sfc", "ingest_ts": None}


def test_la_historia_de_la_antecesora_se_pega_a_la_sucesora():
    p = pl.DataFrame([fila(date(2025, 5, 15), "cob07paao000", 40000.0), fila(date(2025, 5, 16), "cob07paao000", 40100.0),
                      fila(date(2025, 5, 19), "cotk7paao003", 40500.0)], schema=ESQ)
    r = sucesiones.aplicar(p, LISTA).filter(pl.col("especie") == "cotk7paao003")
    assert r.get_column("precio_promedio").to_list() == [40000.0, 40100.0, 40500.0]
    assert r.get_column("fuente_id").to_list()[:2] == ["sucesion:cob07paao000"] * 2


def test_sin_codigo_sfc_usa_el_csv_de_respaldo_y_no_pisa_la_sucesora():
    lista = [{**LISTA[0], "antecesora": {"nemotecnico": "BCOLOMBIA", "especie_sfc": None}}]
    p = pl.DataFrame([fila(date(2025, 5, 19), "cotk7paao003", 40500.0)], schema=ESQ)
    csv = pl.DataFrame({"fecha": [date(2025, 5, 15), date(2025, 5, 16), date(2025, 5, 19)],
                        "precio_promedio": [40000.0, 40100.0, 99999.0], "valor_transado": [None, None, None]},
                       schema={"fecha": pl.Date, "precio_promedio": pl.Float64, "valor_transado": pl.Float64})
    r = sucesiones.aplicar(p, lista, respaldos={"BCOLOMBIA.csv": csv}).filter(pl.col("especie") == "cotk7paao003")
    assert r.get_column("precio_promedio").to_list() == [40000.0, 40100.0, 40500.0]  # el 19-may manda la sucesora
    assert r.get_column("fuente_id").to_list()[0] == "sucesion:csv:BCOLOMBIA.csv"


def test_sin_historia_no_cambia_nada():
    p = pl.DataFrame([fila(date(2025, 5, 19), "cotk7paao003", 40500.0)], schema=ESQ)
    assert sucesiones.aplicar(p, [{**LISTA[0], "antecesora": {"especie_sfc": None}}], respaldos={}).height == 1
