"""Graficador (#/graficador): endpoint /api/graficador con datos sintéticos.

Alineación por fecha sin rellenar huecos, base 100 en el primer dato común, lista cerrada de métricas y
periodos, máximo de acciones, resolución por nemotécnico y uso de fact_valoracion_diaria si existe.
"""

from datetime import date, timedelta

import duckdb
import polars as pl
import pytest

from invest import storage

fastapi = pytest.importorskip("fastapi")

D = [date(2026, 3, 2) + timedelta(days=i) for i in range(5)]  # cinco días seguidos
ESPECIES = {"coa01paao001": ("AAA", "CO-COA01", "Alfa"), "cob02paao002": ("BBB", "CO-COB02", "Beta"),
            "cob02paap003": ("PFBBB", "CO-COB02", "Beta")}
# Precio por especie y día; None = ese día no negoció (no hay fila)
PRECIOS = {"coa01paao001": [10.0, 20.0, 25.0, 30.0, 40.0],
           "cob02paao002": [None, 50.0, 55.0, None, 100.0],
           "cob02paap003": [None, None, 7.0, None, 14.0]}
TRM = [4000.0, 4000.0, 5000.0, 4000.0, 4000.0]


@pytest.fixture
def cliente(carpeta_datos):
    from fastapi.testclient import TestClient

    from invest.app import servidor

    dims = carpeta_datos / "gold" / "dims"
    dims.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"especie": list(ESPECIES), "nemotecnico": [v[0] for v in ESPECIES.values()],
                  "nombre": [f"AO {v[2]}" for v in ESPECIES.values()], "emisor_id": [v[1] for v in ESPECIES.values()],
                  "clase_accion": ["ordinaria", "ordinaria", "preferencial"], "activa": [True] * 3,
                  "clase_activo_id": ["ACCION"] * 3, "pais_id": ["CO"] * 3, "moneda_id": ["COP"] * 3}
                 ).write_parquet(dims / "dim_activo.parquet")
    pl.DataFrame({"emisor_id": ["CO-COA01", "CO-COB02"], "nombre_corto": ["Alfa", "Beta"], "sector_sfc": ["X", "X"]}
                 ).write_parquet(dims / "dim_emisor.parquet")
    precios, retornos = [], []
    for e, serie in PRECIOS.items():
        for i, p in enumerate(serie):
            if p is None:
                continue
            precios.append({"fecha": D[i], "especie": e, "precio": p, "precio_promedio": p, "trm": TRM[i]})
            retornos.append({"fecha": D[i], "especie": e, "precio_promedio": p, "indice_rt": p * 2})  # retorno total = 2 × precio
    storage.escribir_por_anio("gold", "fact_precio_diario", pl.DataFrame(precios))
    storage.escribir_por_anio("gold", "fact_retorno_diario", pl.DataFrame(retornos))
    storage.escribir_por_anio("gold", "fact_indice_mercado", pl.DataFrame(
        [{"fecha": d, "indice_id": "universo_igual", "moneda": m, "valor": 100.0 + i * (10 if m == "COP" else 5)}
         for i, d in enumerate(D) for m in ("COP", "USD")]))
    storage.escribir_por_anio("gold", "fact_macro", pl.DataFrame(
        [{"fecha": d, "serie_id": "trm_usd_cop", "valor": TRM[i]} for i, d in enumerate(D)]))
    # Valoración mensual: dos fines de mes; P/U nulo (utilidad negativa) en un mes de BBB
    storage.escribir_por_anio("gold", "fact_valoracion", pl.DataFrame([
        {"fecha": date(2026, 1, 30), "especie": "coa01paao001", "p_vl": 1.5, "p_u": 10.0, "rendimiento_dividendo_12m": 0.04},
        {"fecha": date(2026, 2, 27), "especie": "coa01paao001", "p_vl": 1.8, "p_u": 12.0, "rendimiento_dividendo_12m": 0.05},
        {"fecha": date(2026, 1, 30), "especie": "cob02paao002", "p_vl": 0.9, "p_u": None, "rendimiento_dividendo_12m": 0.07},
        {"fecha": date(2026, 2, 27), "especie": "cob02paao002", "p_vl": 1.0, "p_u": 8.0, "rendimiento_dividendo_12m": 0.06},
    ]))
    return TestClient(servidor.app, base_url="http://127.0.0.1:8765")


def _get(cliente, **params):
    return cliente.get("/api/graficador", params=params)


def test_alinea_por_fecha_sin_rellenar_huecos(cliente):
    d = _get(cliente, series="coa01paao001,cob02paao002", metrica="precio", periodo="max").json()
    assert d["fechas"] == [x.isoformat() for x in D]
    beta = d["valores"]["cob02paao002"]
    assert beta[0] is None and beta[3] is None  # días sin negocio: vacío, no el precio anterior
    assert [s["observaciones"] for s in d["series"]] == [5, 3]
    assert d["metrica"]["unidad"] == "indice" and d["metrica"]["base100"]


def test_base_100_en_el_primer_dato_comun(cliente):
    d = _get(cliente, series="coa01paao001,cob02paao002", metrica="precio", periodo="max").json()
    assert d["fecha_base"] == D[1].isoformat()  # BBB empieza el segundo día
    alfa, beta = d["valores"]["coa01paao001"], d["valores"]["cob02paao002"]
    assert alfa[1] == pytest.approx(100) and beta[1] == pytest.approx(100)
    assert alfa[0] == pytest.approx(50)  # antes de la base: el valor frente a la base (10 / 20)
    assert alfa[4] == pytest.approx(200) and beta[4] == pytest.approx(200)
    assert any("Base 100" in a for a in d["avisos"])
    # Con una sola serie la base es su primer dato del periodo
    solo = _get(cliente, series="coa01paao001", metrica="retorno_total", periodo="max").json()
    assert solo["fecha_base"] == D[0].isoformat() and solo["valores"]["coa01paao001"][0] == pytest.approx(100)
    assert not any("Base 100" in a for a in solo["avisos"])


def test_retorno_en_dolares_e_indice(cliente):
    d = _get(cliente, series="coa01paao001,universo_igual", metrica="retorno_usd", periodo="max").json()
    alfa = d["valores"]["coa01paao001"]
    # (2 × precio / TRM) normalizado: el tercer día la TRM sube de 4.000 a 5.000
    assert alfa[2] == pytest.approx((50 / 5000) / (20 / 4000) * 100)
    assert d["valores"]["universo_igual"][4] == pytest.approx(120)  # serie en dólares: 100 → 120
    assert next(s for s in d["series"] if s["id"] == "universo_igual")["tipo"] == "indice"


def test_indices_solo_en_base_100_y_valoracion_mensual(cliente):
    d = _get(cliente, series="AAA,BBB,universo_igual", metrica="p_u", periodo="max").json()
    assert [s["id"] for s in d["series"]] == ["coa01paao001", "cob02paao002"]
    assert any("solo se grafican en base 100" in a for a in d["avisos"])
    assert d["metrica"]["fuente"] == "fact_valoracion" and d["metrica"]["frecuencia"] == "mensual"
    assert d["fecha_base"] is None
    assert d["valores"]["cob02paao002"] == [None, 8.0]  # P/U nulo (utilidad negativa): hueco
    r = {x["id"]: x for x in d["resumen"]}
    assert r["coa01paao001"]["variacion"] == pytest.approx(0.2)  # relativa: 12 / 10 - 1
    div = _get(cliente, series="AAA,BBB", metrica="rendimiento_dividendo", periodo="max").json()
    r = {x["id"]: x for x in div["resumen"]}
    assert r["cob02paao002"]["variacion"] == pytest.approx(-0.01)  # en puntos: 6 % - 7 %
    assert div["metrica"]["unidad"] == "porcentaje"


def test_resumen_del_periodo(cliente):
    d = _get(cliente, series="cob02paao002", metrica="precio", periodo="max").json()
    r = d["resumen"][0]
    assert r["inicial"] == pytest.approx(100) and r["final"] == pytest.approx(200)
    assert r["variacion"] == pytest.approx(1.0)
    assert (r["maximo"], r["fecha_maximo"]) == (pytest.approx(200), D[4].isoformat())
    assert (r["minimo"], r["fecha_minimo"]) == (pytest.approx(100), D[1].isoformat())
    assert r["observaciones"] == 3


def test_periodo_recorta_desde_la_ultima_fecha(cliente):
    d = _get(cliente, series="AAA", metrica="p_vl", periodo="6m").json()
    assert d["periodo"]["hasta"] == "2026-02-27" and d["periodo"]["desde"] == "2025-08-29"
    corto = _get(cliente, series="AAA", metrica="precio", periodo="6m").json()
    assert len(corto["fechas"]) == 5


def test_lista_cerrada_de_metricas_y_periodos(cliente):
    r = _get(cliente, series="AAA", metrica="precio; DROP TABLE x", periodo="1a")
    assert r.status_code == 400 and "Métrica no permitida" in r.json()["detail"]
    assert _get(cliente, series="AAA", metrica="precio", periodo="2a").status_code == 400
    assert _get(cliente, series="", metrica="precio", periodo="1a").status_code == 400
    o = cliente.get("/api/graficador/opciones").json()
    assert {m["id"] for m in o["metricas"]} == {"precio", "retorno_total", "retorno_usd", "p_vl", "p_u",
                                                "rendimiento_dividendo", "volatilidad_60", "puntaje_global"}
    assert [i["id"] for i in o["indices"]] == ["universo_igual"]  # sin COLCAP en fact_macro, no se ofrece
    assert o["max_acciones"] == 6


def test_maximo_seis_acciones(cliente):
    siete = ",".join(["AAA", "BBB", "PFBBB", "X1", "X2", "X3", "X4"])
    r = _get(cliente, series=siete, metrica="precio", periodo="1a")
    assert r.status_code == 400 and "Máximo 6" in r.json()["detail"]
    # Los índices no cuentan entre las seis; repetidas cuentan una vez
    ok = _get(cliente, series="AAA,aaa,coa01paao001,BBB,universo_igual", metrica="precio", periodo="1a")
    assert ok.status_code == 200 and len(ok.json()["series"]) == 3


def test_resuelve_por_nemotecnico_y_nombre(cliente):
    d = _get(cliente, series="aaa,Beta,PFBBB", metrica="precio", periodo="max").json()
    assert [s["id"] for s in d["series"]] == ["coa01paao001", "cob02paao002", "cob02paap003"]
    # Ordinaria y preferencial del mismo emisor: el nombre corto lleva el nemotécnico
    assert [s["nombre"] for s in d["series"]] == ["Alfa", "Beta (BBB)", "Beta (PFBBB)"]
    r = _get(cliente, series="AAA,NOEXISTE", metrica="precio", periodo="max")
    assert r.status_code == 404 and "NOEXISTE" in r.json()["detail"]


def test_usa_valoracion_diaria_si_existe(cliente):
    from invest.app import servidor

    assert _get(cliente, series="AAA", metrica="p_vl", periodo="max").json()["metrica"]["fuente"] == "fact_valoracion"
    # Otra tabla del modelo (fact_valoracion_diaria: especie, fecha, p_vl, p_u) aparece en la base
    with duckdb.connect(str(servidor.ruta_base())) as con:
        con.execute("""CREATE TABLE fact_valoracion_diaria AS SELECT * FROM (VALUES
                       ('coa01paao001', DATE '2026-03-02', 1.9, 13.0), ('coa01paao001', DATE '2026-03-03', 2.0, 14.0))
                       t(especie, fecha, p_vl, p_u)""")
    d = _get(cliente, series="AAA", metrica="p_vl", periodo="max").json()
    assert d["metrica"]["fuente"] == "fact_valoracion_diaria" and d["metrica"]["frecuencia"] == "diaria"
    assert d["fechas"] == ["2026-03-02", "2026-03-03"] and d["valores"]["coa01paao001"] == [1.9, 2.0]
    # El rendimiento del dividendo sigue saliendo de la mensual
    assert _get(cliente, series="AAA", metrica="rendimiento_dividendo", periodo="max").json()["metrica"]["fuente"] == "fact_valoracion"


def test_pagina_en_el_menu(cliente):
    html = cliente.get("/").text
    assert 'href="#/graficador"' in html
    js = cliente.get("/static/app.js").text
    assert "paginas.graficador" in js and "Comparar en el graficador" in js
