"""Plantilla de bancos: métricas con el CUIF (gold/fact_metrica_banco), puntajes y ficha."""

from __future__ import annotations

import os
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from invest import bancos, modelo, puntajes, storage
from invest.config import CONFIG_DIR, cargar_yaml
from invest.ingest import cuif

FOTO = Path(os.environ.get("INVEST_FOTO_DIR", "/home/claude/foto"))

# Flujos mensuales (se acumulan en el año, como en el CUIF) y saldos de un banco sintético
FLUJOS = {
    "590000": 10.0,                                  # utilidad: 120 al año
    "410200": 20.0, "410300": 10.0,                  # intereses recibidos: 360 al año
    "510200": 8.0, "510300": 3.0, "510400": 1.0,     # intereses pagados: 144 al año
    "415000": 2.5,                                   # utilidad de filiales: 30 al año (25 % de la utilidad)
    "410000": 100.0, "419800": 2.0, "510000": 85.0, "517000": 4.0, "517100": 1.0,
    "512000": 6.0, "513000": 1.0, "519000": 3.0,     # gastos administrativos: 10 al mes
    "517005": 3.0, "517010": 0.5, "517105": 0.3, "517115": 0.2,   # deterioro de cartera: 4 al mes
    "419810": 1.0, "419855": 0.5,                    # recuperaciones de cartera: 1,5 al mes
}
SALDOS = {
    "100000": 10_000.0, "300000": 1_000.0, "191100": 200.0, "120000": 0.0, "130000": 0.0,
    "140400": 1_000.0, "140800": 2_000.0, "141000": 3_000.0, "141200": 0.0,
    "140810": 100.0, "140815": 50.0, "140820": 30.0, "140825": 20.0, "141010": 100.0,
    "149100": -150.0, "149500": -90.0,               # deterioro acumulado (cuentas correctoras)
}


def _meses(desde: date, n: int) -> list[date]:
    salida, a, m = [], desde.year, desde.month
    for _ in range(n):
        siguiente = date(a + (m == 12), m % 12 + 1, 1)
        salida.append(siguiente - timedelta(days=1))
        a, m = siguiente.year, siguiente.month
    return salida


def cuif_sintetico(entidades=("7",), desde=date(2023, 1, 1), meses=36, solo_primer_nivel=(), escala=None) -> pl.DataFrame:
    filas = []
    for e in entidades:
        k = (escala or {}).get(e, 1.0)
        for i, f in enumerate(_meses(desde, meses)):
            valores = {c: v * f.month * k for c, v in FLUJOS.items()}
            valores |= {c: v * k for c, v in SALDOS.items()}
            valores["210800"] = 5_000.0 * k * 1.01 ** i  # depósitos de ahorro: crecen 1 % al mes
            for c, v in valores.items():
                if e in solo_primer_nivel and not c.endswith("0000"):
                    continue
                filas.append({"fecha_corte": f, "codigo_entidad": e, "nombre_entidad": f"BANCO {e}", "cuenta": c,
                              "nombre_cuenta": c, "valor": v})
    return pl.DataFrame(filas)


def _cfg(mapeo: list[dict]) -> dict:
    return {"agregados": bancos.config()["agregados"], "mapeo": mapeo}


def _valor(df: pl.DataFrame, emisor: str, metrica: str, corte: date) -> float | None:
    s = df.filter((pl.col("emisor_id") == emisor) & (pl.col("metrica") == metrica) & (pl.col("fecha_corte") == corte))
    return s.item(0, "valor") if s.height else None


# --------------------------------------------------------------------------- métricas
def test_metricas_de_un_banco():
    cfg = _cfg([{"emisor_id": "CO-B7", "nombre": "Banco 7", "entidades": ["7"]}])
    df = bancos.fact_metrica_banco(cuif_sintetico(), cfg, rezago=50)
    corte = date(2025, 6, 30)
    v = lambda m: _valor(df, "CO-B7", m, corte)  # noqa: E731
    assert v("utilidad_12m") == pytest.approx(120)
    assert v("roe_banco_12m") == pytest.approx(0.12)
    assert v("roa_banco_12m") == pytest.approx(0.012)
    assert v("patrimonio_tangible") == pytest.approx(800)
    assert v("rote_12m") == pytest.approx(0.15)
    assert v("cartera_bruta") == pytest.approx(6_000)
    assert v("nim_12m") == pytest.approx(216 / 6_000)
    # margen financiero bruto mensual = (100 − 2) − (85 − 10 − 5) = 28 = utilidad antes de impuestos + adm. + deterioro neto
    assert v("eficiencia_12m") == pytest.approx(10 / 28)
    assert v("costo_riesgo_12m") == pytest.approx(12 * 2.5 / 6_000)
    assert v("indice_cartera_riesgo") == pytest.approx(300 / 6_000)
    assert v("indice_cartera_cde") == pytest.approx(100 / 6_000)
    assert v("cobertura_cartera_cde") == pytest.approx(240 / 100)  # valor absoluto del deterioro
    assert v("crecimiento_depositos_12m") == pytest.approx(1.01 ** 12 - 1)
    assert v("crecimiento_cartera_12m") == pytest.approx(0)
    assert v("utilidad_filiales_12m") == pytest.approx(30 / 120)
    assert set(df.get_column("metrica")) == set(bancos.METRICAS)
    # Fecha disponible = corte + rezago; los 12 meses exigen el año anterior y los promedios 13 cierres
    assert (df.get_column("fecha_disponible") == df.get_column("fecha_corte") + timedelta(days=50)).all()
    roe = df.filter(pl.col("metrica") == "roe_banco_12m")
    assert roe.get_column("fecha_corte").min() == date(2024, 1, 31)
    assert (roe.get_column("es_holding") == False).all() and (roe.get_column("base") == "cuif_banco").all()  # noqa: E712
    assert roe.filter(pl.col("fecha_corte") == corte).item(0, "unidad") == "ratio"
    assert df.filter(pl.col("metrica") == "cartera_bruta").item(0, "unidad") == "pesos"


def test_cuentas_faltantes_dejan_la_metrica_vacia_sin_romper():
    """Con solo el primer nivel (como la foto actual) salen ROE, ROA y utilidad; el resto queda vacío."""
    cfg = _cfg([{"emisor_id": "CO-B13", "entidades": ["13"]}])
    df = bancos.fact_metrica_banco(cuif_sintetico(("13",), solo_primer_nivel=("13",)), cfg, rezago=50)
    assert set(df.get_column("metrica")) == {"utilidad_12m", "roe_banco_12m", "roa_banco_12m"}
    assert bancos.fact_metrica_banco(pl.DataFrame(schema={"fecha_corte": pl.Date, "codigo_entidad": pl.Utf8,
                                                          "cuenta": pl.Utf8, "valor": pl.Float64}), cfg).is_empty()


def test_codigos_de_cuatro_digitos_se_normalizan():
    c = cuif_sintetico().with_columns(pl.col("cuenta").str.replace(r"^(\d{4})00$", "$1"))
    assert "1408" in c.get_column("cuenta").to_list()
    df = bancos.fact_metrica_banco(c, _cfg([{"emisor_id": "CO-B7", "entidades": ["7"]}]), rezago=50)
    assert _valor(df, "CO-B7", "cartera_bruta", date(2025, 6, 30)) == pytest.approx(6_000)


def test_holding_suma_de_bancos_y_pares_sin_emisor():
    cuif_df = cuif_sintetico(("1", "2", "7", "12"), escala={"2": 3.0})
    # A la entidad 2 le falta un mes: la suma del grupo no existe ese mes
    hueco = date(2025, 3, 31)
    cuif_df = cuif_df.filter(~((pl.col("codigo_entidad") == "2") & (pl.col("fecha_corte") == hueco)))
    cfg = _cfg([
        {"emisor_id": "CO-AVAL", "nombre": "Aval", "entidades": ["1", "2"], "holding": True},
        {"emisor_id": "CO-CIB", "entidades": ["7"], "holding": True, "holding_desde": date(2025, 5, 19)},
        {"emisor_id": None, "nombre": "GNB", "entidades": ["12"]},
    ])
    df = bancos.fact_metrica_banco(cuif_df, cfg, rezago=50)
    aval = df.filter(pl.col("emisor_id") == "CO-AVAL")
    assert (aval.get_column("base") == "cuif_suma_bancos").all() and aval.get_column("es_holding").all()
    assert aval.item(0, "entidades") == "1,2"
    assert _valor(df, "CO-AVAL", "cartera_bruta", date(2025, 6, 30)) == pytest.approx(6_000 * 4)
    assert _valor(df, "CO-AVAL", "cartera_bruta", hueco) is None
    assert _valor(df, "CO-AVAL", "roe_banco_12m", date(2025, 2, 28)) == pytest.approx(0.12)  # razones de la suma
    cib = df.filter((pl.col("emisor_id") == "CO-CIB") & (pl.col("metrica") == "roe_banco_12m"))
    assert not cib.filter(pl.col("fecha_corte") < date(2025, 5, 19)).get_column("es_holding").any()
    assert cib.filter(pl.col("fecha_corte") >= date(2025, 5, 31)).get_column("es_holding").all()
    assert "CUIF-12" in df.get_column("emisor_id").to_list()
    assert df.group_by(["emisor_id", "fecha_corte", "metrica"]).len().get_column("len").max() == 1


# --------------------------------------------------------------------------- configuración y conector
def test_cuentas_de_la_plantilla_estan_en_el_conector():
    fuente = next(f for f in cargar_yaml("catalogo_fuentes.yaml")["fuentes"] if f["id"] == "cuif_datos_gov")
    extra = [str(c) for c in fuente["cuentas_extra"]]
    assert len(extra) == len(set(extra)) and all(re.fullmatch(r"\d{6}", c) for c in extra)
    usadas = {c for c in bancos.cuentas_usadas() if not c.endswith("0000")}
    assert usadas <= set(extra), usadas - set(extra)
    assert bancos.rezago_dias() == fuente["rezago_dias"] == 50
    # Cada cuenta extra lleva su comentario en el catálogo
    texto = (CONFIG_DIR / "catalogo_fuentes.yaml").read_text(encoding="utf-8")
    for c in extra:
        assert re.search(rf'- "{c}"\s+# \S', texto), c


def test_mapeo_apunta_a_emisores_conocidos():
    rnve = {e["emisor_id"] for e in cargar_yaml("emisores_rnve.yaml")["emisores"]}
    entidades = {str(e["codigo"]) for f in cargar_yaml("catalogo_fuentes.yaml")["fuentes"] if f["id"] == "cuif_datos_gov"
                 for e in f["entidades"]}
    for m in bancos.config()["mapeo"]:
        assert set(map(str, m["entidades"])) <= entidades, m
        if m.get("emisor_id"):
            assert m["emisor_id"] in rnve, m["emisor_id"]
        if m.get("holding_desde"):
            assert m.get("holding")
    holdings = {m["emisor_id"] for m in bancos.config()["mapeo"] if m.get("holding")}
    assert holdings == {"CO-COTK7", "CO-PAK01", "CO-COT29"}


def test_conector_pide_las_cuentas_y_rellena_el_historico_una_vez(carpeta_datos, monkeypatch):
    vistos = []

    def consultar(url, where=None, order=None, timeout=60, **kw):
        vistos.append(where)
        f = re.search(r"fecha_corte >= '(\d{4}-\d{2}-\d{2})", where).group(1)
        return [{"tipo_entidad": "1", "codigo_entidad": "7", "nombre_entidad": "B", "fecha_corte": f + "T00:00:00.000",
                 "cuenta": c, "nombre_cuenta": c, "moneda": "0", "signo_valor": "+", "valor": v}
                for c, v in (("100000", "10"), ("200000", "6"), ("300000", "4"), ("140800", "5"))]

    monkeypatch.setattr(cuif.socrata, "consultar", consultar)
    from datetime import datetime, timezone

    storage.upsert_por_anio("silver", "fact_cuif_mensual", pl.DataFrame({
        "fecha_corte": [date(2026, 8, 31)], "codigo_entidad": ["7"], "nombre_entidad": ["B"], "cuenta": ["100000"],
        "nombre_cuenta": ["A"], "valor": [1.0], "fuente_id": ["cuif_datos_gov"],
        "ingest_ts": [datetime.now(timezone.utc)]}, schema=cuif.ESQUEMA), ["fecha_corte", "codigo_entidad", "cuenta"],
        "fecha_corte")
    con = cuif.ConectorCUIF()
    # Hay datos pero las cuentas extra nunca se bajaron: todo el histórico
    assert con.rango_por_defecto(date(2026, 11, 24))[0] == date(2015, 1, 1)
    res = con.ejecutar()
    assert res.estado != "error", res.detalle
    assert "cuenta in('140400','140800'" in vistos[-1] and "'419855'" in vistos[-1]
    assert storage.leer_estado_fuente("cuif_datos_gov")["cuentas_extra"] == sorted(con._extra())
    # Ya bajadas: vuelve a la recarga normal (último dato − 100 días)
    desde, _ = cuif.ConectorCUIF().rango_por_defecto(date(2026, 11, 24))
    assert desde == date(2026, 8, 31) - timedelta(days=100)


# --------------------------------------------------------------------------- puntajes
def _panel_bancos() -> pl.DataFrame:
    filas = []
    f = date(2025, 6, 30)
    for k in range(10):  # 10 empresas no financieras
        filas.append({"fecha": f, "especie": f"e{k}", "emisor_id": f"E{k}", "sector_sfc": "OTRO", "en_universo": True,
                      "roe_12m": 0.05 + 0.01 * k, "apalancamiento": 0.3 + 0.02 * k, "entidad_banco": None})
    for k in range(6):  # 6 bancos (el último sin ROTE: usa el ROE del CUIF)
        filas.append({"fecha": f, "especie": f"b{k}", "emisor_id": f"B{k}", "sector_sfc": "BC-ESTABLECIMIENTO BANCARIO",
                      "en_universo": k < 3, "roe_12m": 0.2, "apalancamiento": 0.9, "entidad_banco": str(k),
                      "rote_12m": None if k == 5 else 0.10 + 0.02 * k, "roe_banco_12m": 0.11 + 0.02 * k,
                      "costo_riesgo_12m": 0.02, "eficiencia_12m": 0.40 + 0.02 * k})
    # Un holding (otro sector) con el mismo banco que b0: entra por el mapeo y no duplica al par
    filas.append({"fecha": f, "especie": "h0", "emisor_id": "H0", "sector_sfc": "SOCIEDADES INVERSORAS",
                  "en_universo": True, "roe_12m": 0.1, "apalancamiento": 0.2, "entidad_banco": "0", "rote_12m": 0.10,
                  "roe_banco_12m": 0.11, "costo_riesgo_12m": 0.02, "eficiencia_12m": 0.40})
    base = {"nombre": "x", "clase_accion": "ordinaria", "precio": 100.0, "capitalizacion": 1e9, "capitalizacion_usd": 1e6,
            "p_vl": 1.0, "p_u": None, "rendimiento_utilidad": None, "rendimiento_dividendo_12m": None,
            "crecimiento_upa_12m": None, "crecimiento_ingresos_12m": None, "momentum_12_1": 0.1,
            "momentum_12_1_percentil": 0.5, "distancia_sma200": None, "tendencia_alcista": True, "caida_desde_max_52s": -0.1,
            "rsi_14": 50.0, "tecnico_valido": True, "volatilidad_anual": None, "drawdown_maximo": None, "beta": None,
            "percentil_validado": None, "tercio_validado": None}
    return pl.DataFrame([base | r for r in filas], infer_schema_length=None)


def test_calidad_de_los_bancos_con_metricas_de_banco():
    cfg = puntajes.config()
    puntaje, matriz = puntajes.calcular(_panel_bancos(), cfg, validados=set())
    m = matriz.sort("especie")
    assert m.filter(pl.col("especie").str.starts_with("b")).get_column("es_banco").all()
    assert m.filter(pl.col("especie") == "h0").get_column("es_banco").item()
    assert not m.filter(pl.col("especie").str.starts_with("e")).get_column("es_banco").any()
    calidad = puntaje.filter(pl.col("factor") == "calidad")
    # Los bancos no usan ROE de anexos ni apalancamiento; los demás no usan métricas de banco
    banco = calidad.filter(pl.col("especie") == "b2")
    assert set(banco.filter(pl.col("nivel") == "metrica").get_column("elemento")) == {
        "rote_12m", "costo_riesgo_12m", "eficiencia_12m"}
    otro = calidad.filter((pl.col("especie") == "e2") & (pl.col("nivel") == "metrica"))
    assert set(otro.get_column("elemento")) == {"roe_12m", "apalancamiento"}
    assert set(otro.get_column("grupo_comparacion")) == {"OTRO"}  # 10 pares del sector: alcanza el mínimo de 8
    # Pares: solo bancos, una vez por entidad (h0 comparte banco con b0): 5 con ROTE
    rote = banco.filter(pl.col("elemento") == "rote_12m").row(0, named=True)
    assert rote["grupo_comparacion"] == "bancos" and rote["pares"] == 5 and rote["z"] == pytest.approx(0)
    # Costo del riesgo igual en todos: sin dispersión no hay z (excluido, no 5); eficiencia con dirección negativa
    assert banco.filter(pl.col("elemento") == "costo_riesgo_12m").item(0, "z") is None
    ef = calidad.filter((pl.col("elemento") == "eficiencia_12m") & pl.col("especie").is_in(["b0", "b4"])).sort("especie")
    assert ef.item(0, "z") > 0 > ef.item(1, "z")
    # Respaldo: b5 sin ROTE usa el ROE del CUIF
    b5 = calidad.filter((pl.col("especie") == "b5") & (pl.col("nivel") == "metrica"))
    assert set(b5.get_column("elemento")) == {"rote_12m", "roe_banco_12m", "costo_riesgo_12m", "eficiencia_12m"}
    assert b5.filter(pl.col("elemento") == "rote_12m").item(0, "valor") is None
    assert b5.filter(pl.col("elemento") == "roe_banco_12m").item(0, "z") is not None
    assert "roe_banco_12m" not in set(banco.get_column("elemento"))  # con ROTE no se usa el respaldo
    assert m.filter(pl.col("especie") == "b5").item(0, "metricas_calidad") == 2
    # El apalancamiento de los bancos (0,9) ya no cuenta en su calidad
    _, otra = puntajes.calcular(_panel_bancos().with_columns(
        pl.when(pl.col("especie").str.starts_with("b")).then(0.1).otherwise(pl.col("apalancamiento")).alias("apalancamiento")),
        cfg, validados=set())
    assert otra.sort("especie").get_column("puntaje_calidad").to_list() == m.get_column("puntaje_calidad").to_list()


def test_pocos_bancos_dejan_la_metrica_vacia():
    cfg = puntajes.config()
    p = _panel_bancos().filter(~pl.col("especie").is_in(["b3", "b4", "b5"]))  # 3 entidades < mínimo de 5
    puntaje, matriz = puntajes.calcular(p, cfg, validados=set())
    assert matriz.filter(pl.col("es_banco")).get_column("puntaje_calidad").is_null().all()
    assert matriz.filter(~pl.col("es_banco")).get_column("puntaje_calidad").is_not_null().all()


def test_referencia_con_todas_las_entidades_del_cuif():
    """La referencia sale de fact_metrica_banco (incluye bancos sin precio) y no cuenta dos veces un banco."""
    cuif_df = cuif_sintetico(tuple(str(k) for k in range(6)), escala={str(k): 1 + k for k in range(6)})
    cfg = _cfg([{"emisor_id": f"B{k}", "entidades": [str(k)]} for k in range(6)]
               + [{"emisor_id": "H0", "entidades": ["0"], "holding": True},
                  {"emisor_id": "G", "entidades": ["0", "1"], "holding": True}])
    mb = bancos.fact_metrica_banco(cuif_df, cfg, rezago=50)
    fecha = date(2025, 9, 30)
    ref = puntajes.referencia_bancos([fecha], puntajes.config(), mb)
    rote = ref.filter(pl.col("metrica") == "rote_12m").row(0, named=True)
    assert rote["_pares"] == 6  # B0..B5; H0 repite la entidad 0 y G es una suma
    p = _panel_bancos().with_columns(pl.lit(fecha).alias("fecha"))
    puntaje, _ = puntajes.calcular(p, puntajes.config(), validados=set(), ref_bancos=ref)
    assert puntaje.filter(pl.col("elemento") == "rote_12m").get_column("pares").drop_nulls().unique().to_list() == [6]


# --------------------------------------------------------------------------- modelo y aplicativo
def test_paso_del_modelo_y_contrato(carpeta_datos, monkeypatch):
    assert "bancos" in modelo.PASOS and list(modelo.PASOS).index("bancos") < list(modelo.PASOS).index("puntajes")
    assert ("gold", "fact_metrica_banco") in modelo.PASOS["puntajes"]["entradas"]
    assert bancos.construir() == {"metricas": 0}  # sin CUIF: tabla vacía, el contrato la encuentra
    assert not [x for x in modelo.verificar() if x.startswith("fact_metrica_banco")]
    storage.upsert_por_anio("silver", "fact_cuif_mensual", cuif_sintetico(("7", "13")),
                            ["fecha_corte", "codigo_entidad", "cuenta"], "fecha_corte")
    r = bancos.construir()
    assert r["emisores"] == 2 and r["ultimo_corte"] == date(2025, 12, 31)  # Bancolombia/Cibest y BBVA
    assert not [x for x in modelo.verificar() if x.startswith("fact_metrica_banco")]


def test_ficha_banco(carpeta_datos):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from invest.app import servidor

    carpeta = carpeta_datos / "gold" / "dims"
    carpeta.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"especie": ["cotk7paao003", "coc04paao008"], "nemotecnico": ["CIBEST", "ECOPETROL"],
                  "nombre": ["AO CIBEST", "AO ECOPETROL"], "emisor_id": ["CO-COTK7", "CO-COC04"]}).write_parquet(
        carpeta / "dim_activo.parquet")
    storage.upsert_por_anio("silver", "fact_cuif_mensual", cuif_sintetico(("7", "39", "12")),
                            ["fecha_corte", "codigo_entidad", "cuenta"], "fecha_corte")
    bancos.construir()
    modelo.construir_duckdb()
    c = TestClient(servidor.app, base_url="http://127.0.0.1:8765")
    b = c.get("/api/ficha/cotk7paao003/banco").json()
    assert b["tiene"] and b["es_holding"] and b["fecha_corte"] == "2025-12-31" and b["corte_anterior"] == "2024-12-31"
    assert b["actual"]["rote_12m"] == pytest.approx(0.15) and b["anterior"]["roe_banco_12m"] == pytest.approx(0.12)
    assert "Cibest" in (b["nota"] or "")
    assert {r["entidades"] for r in b["pares"]} == {"7", "39", "12"}  # Davivienda y Davivienda Group: un par
    assert [r for r in b["pares"] if r["propio"]][0]["emisor_id"] == "CO-COTK7"
    assert b["serie"] and b["serie"][-1]["fecha_corte"] == "2025-12-31"
    assert c.get("/api/ficha/coc04paao008/banco").json() == {"tiene": False}
    assert c.get("/api/ficha/__no_existe__/banco").json() == {"tiene": False}


# --------------------------------------------------------------------------- foto real
@pytest.mark.skipif(not (FOTO / "silver" / "fact_cuif_mensual").exists(), reason="sin foto real de los datos")
def test_foto_real(tmp_path, monkeypatch):
    destino = tmp_path / "invest-data"
    shutil.copytree(FOTO / "silver" / "fact_cuif_mensual", destino / "silver" / "fact_cuif_mensual")
    monkeypatch.setenv("INVEST_DATA_DIR", str(destino))
    df = bancos.fact_metrica_banco()
    assert not df.is_empty()
    ultimo = df.get_column("fecha_corte").max()
    hoy = df.filter(pl.col("fecha_corte") == ultimo)
    esperados = {m["emisor_id"] or "CUIF-" + "+".join(m["entidades"]) for m in bancos.config()["mapeo"]}
    assert set(hoy.get_column("emisor_id")) == esperados
    roe = hoy.filter(pl.col("metrica") == "roe_banco_12m")
    assert roe.height == len(esperados) and roe.get_column("valor").is_between(-0.5, 1.0).all()
    roa = hoy.filter(pl.col("metrica") == "roa_banco_12m")
    assert roa.get_column("valor").is_between(-0.05, 0.1).all()
    assert (df.get_column("fecha_disponible") - df.get_column("fecha_corte")).dt.total_days().unique().to_list() == [50]
    # Davivienda y Davivienda Group comparten banco
    dav = hoy.filter(pl.col("metrica") == "roe_banco_12m", pl.col("emisor_id").is_in(["CO-COB51", "CO-PAK01"]))
    assert dav.get_column("valor").n_unique() == 1
    dims = FOTO / "gold" / "dims" / "dim_emisor.parquet"
    if dims.exists():
        conocidos = set(pl.read_parquet(dims).get_column("emisor_id"))
        assert {e for e in esperados if not e.startswith("CUIF-")} <= conocidos
    # Cuando lleguen las cuentas extra: cartera bruta − deterioro ≈ grupo 14 (que es neto)
    c = bancos._normalizar(pl.read_parquet(destino / "silver" / "fact_cuif_mensual" / "**" / "*.parquet"))
    if c.filter(pl.col("cuenta") == "140800").height:
        neto = c.filter(pl.col("cuenta") == "140000").select("codigo_entidad", "fecha_corte", pl.col("valor").alias("g14"))
        ag = bancos.agregados_entidad(c).pivot(on="agregado", index=["codigo_entidad", "fecha_corte"], values="valor")
        t = ag.join(neto, on=["codigo_entidad", "fecha_corte"]).with_columns(
            ((pl.col("cartera_bruta") - pl.col("deterioro_cartera")) / pl.col("g14") - 1).abs().alias("_d"))
        assert t.get_column("_d").median() < 0.05
