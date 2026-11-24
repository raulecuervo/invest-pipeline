import math
from datetime import date, timedelta

import numpy as np
import polars as pl

from invest import macro, mercado, metricas, riesgo, storage, tecnico
from invest.calendario import sesiones_entre


def _dims(carpeta, especies):
    (carpeta / "gold" / "dims").mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "activo_id": [e.upper() for e in especies], "especie": especies,
        "nombre": [f"AO EMPRESA {i} S.A." for i in range(len(especies))],
        "emisor_id": [f"CO-{e[:5].upper()}" for e in especies],
        "clase_accion": ["ordinaria"] * len(especies), "sector_sfc": ["BANCOS"] * len(especies),
        "activa": [True] * len(especies), "dias_con_negociacion": [100] * len(especies),
    }).write_parquet(carpeta / "gold" / "dims" / "dim_activo.parquet")


def _trm(sesiones, tasa=lambda i: 4000.0):
    filas = [{"fecha": d + timedelta(days=1), "moneda_origen": "USD", "moneda_destino": "COP", "tasa": tasa(i),
              "fecha_mercado": d, "fuente_id": "trm"} for i, d in enumerate(sesiones)]
    storage.upsert_por_anio("silver", "fact_fx_diaria", pl.DataFrame(filas), ["fecha"])


def _precios(sesiones, especies, retorno):
    filas = []
    for k, e in enumerate(especies):
        x = 1000.0
        for i, d in enumerate(sesiones):
            r = retorno(k, i)
            x *= 1 + r
            filas.append({"fecha": d, "especie": e, "precio_promedio": x, "valor_transado": 1e9, "dias_desde_anterior": 1,
                          "dividendo": None, "distribucion": None, "split_factor": None, "retorno_precio": r,
                          "retorno_total": r, "indice_rt": 100.0, "salto_atipico": False})
    storage.escribir_por_anio("gold", "fact_retorno_diario", pl.DataFrame(filas), "fecha")
    liq = [{"fecha": f["fecha"], "especie": f["especie"], "liquida": True} for f in filas]
    storage.escribir_por_anio("gold", "fact_liquidez_diaria", pl.DataFrame(liq), "fecha")


def test_capitalizacion_con_acciones_conocidas_a_la_fecha(carpeta_datos):
    sesiones = sesiones_entre(date(2025, 1, 1), date(2025, 12, 31))
    _dims(carpeta_datos, ["coa01paao001"])
    _trm(sesiones)
    _precios(sesiones, ["coa01paao001"], lambda k, i: 0.0)
    storage.escribir_completa("gold", "fact_fundamental", pl.DataFrame([
        {"emisor_id": "CO-COA01", "fecha_corte": date(2025, 3, 31), "fecha_disponible": date(2025, 5, 30),
         "concepto": "acciones_circulacion", "valor": 1e6, "unidad": "acciones", "base": "separado", "fuente": "anexo_211"},
        {"emisor_id": "CO-COA01", "fecha_corte": date(2025, 9, 30), "fecha_disponible": date(2025, 11, 29),
         "concepto": "acciones_ordinarias", "valor": 2e6, "unidad": "acciones", "base": "separado", "fuente": "anexo_211"},
    ]))
    df = mercado.fact_precio_diario()
    antes = df.filter(pl.col("fecha") == date(2025, 5, 2)).row(0, named=True)
    medio = df.filter(pl.col("fecha") == date(2025, 6, 3)).row(0, named=True)
    fin = df.filter(pl.col("fecha") == date(2025, 12, 1)).row(0, named=True)
    assert antes["capitalizacion"] is None  # todavía no se había publicado ningún dato
    assert medio["capitalizacion"] == 1000.0 * 1e6  # solo el total: vale para la ordinaria
    assert fin["capitalizacion"] == 1000.0 * 2e6 and fin["capitalizacion_usd"] == 1000.0 * 2e6 / 4000.0


def test_macro_inflacion_y_trm(carpeta_datos):
    meses = [date(2023 + (m // 12), m % 12 + 1, 1) for m in range(24)]
    storage.upsert_por_anio("silver", "fact_serie_macro/fuente=dane_ipc", pl.DataFrame([
        {"fecha": f, "serie_id": "ipc_co", "valor": 100 * 1.01 ** i, "unidad": "indice", "frecuencia": "mensual",
         "fecha_publicacion": f + timedelta(days=40), "publicacion_estimada": False, "fuente_id": "dane_ipc"}
        for i, f in enumerate(meses)]), ["fecha", "serie_id"])
    _trm([date(2024, 1, 2), date(2024, 1, 3)])
    resumen = macro.construir()
    assert resumen["sin_clasificar"] == 0
    df = storage.leer_tabla("gold", "fact_macro")
    inf = df.filter((pl.col("serie_id") == "inflacion_co_12m") & (pl.col("fecha") == date(2024, 6, 1))).row(0, named=True)
    assert math.isclose(inf["valor"], (1.01 ** 12 - 1) * 100, rel_tol=1e-9)
    assert inf["fecha_publicacion"] == date(2024, 6, 1) + timedelta(days=40)
    trm = df.filter(pl.col("serie_id") == "trm_usd_cop").sort("fecha").row(0, named=True)
    assert trm["fecha_publicacion"] == date(2024, 1, 2)  # se publica la tarde de su fecha de mercado


def test_tecnico_riesgo_y_beta(carpeta_datos):
    sesiones = sesiones_entre(date(2021, 1, 1), date(2025, 12, 31))
    especies = [f"cob{k:02d}paao001" for k in range(6)]
    _dims(carpeta_datos, especies)
    _trm(sesiones)
    rnd = np.random.default_rng(1)
    mercado_r = rnd.normal(0.0004, 0.01, len(sesiones))
    ruido = rnd.normal(0, 0.005, (len(especies), len(sesiones)))
    # La acción k tiene beta 0,5 + 0,2k frente al factor común
    _precios(sesiones, especies, lambda k, i: (0.5 + 0.2 * k) * mercado_r[i] + ruido[k, i])
    mercado.construir()
    tecnico.construir()
    t = storage.leer_tabla("gold", "fact_tecnico_diario")
    ultimo = t.filter(pl.col("fecha") == sesiones[-1])
    assert ultimo.height == 6 and ultimo.get_column("rsi_14").is_between(0, 100).all()
    assert (ultimo.get_column("caida_desde_max_52s") <= 0).all()
    riesgo.construir()
    r = storage.leer_tabla("gold", "fact_riesgo").filter(
        (pl.col("fecha") == sesiones[-1]) & (pl.col("ventana") == "3a") & (pl.col("moneda") == "COP")).sort("especie")
    betas = r.get_column("beta").to_list()
    assert betas == sorted(betas)  # más exposición al factor común, más beta
    assert (r.get_column("drawdown_maximo") <= 0).all() and (r.get_column("cvar_95") <= r.get_column("var_95")).all()
    # Pesos iguales: la beta promedio frente al universo es cercana a 1
    assert abs(sum(betas) / len(betas) - 1) < 0.1


def test_doce_meses_y_payout():
    f = pl.DataFrame([
        {"emisor_id": "E", "fecha_corte": c, "fecha_disponible": c + timedelta(days=60), "concepto": "utilidad_accion",
         "valor": v, "unidad": "pesos_por_accion", "base": "separado", "fuente": "a"}
        for c, v in ((date(2024, 9, 30), 60.0), (date(2024, 12, 31), 100.0), (date(2025, 9, 30), 90.0),
                     (date(2025, 12, 31), 120.0))
    ] + [{"emisor_id": "E", "fecha_corte": date(2025, 6, 30), "fecha_disponible": date(2025, 8, 29),
          "concepto": "dividendo_accion_ordinaria", "valor": 50.0, "unidad": "pesos_por_accion", "base": "separado",
          "fuente": "a"},
         {"emisor_id": "E", "fecha_corte": date(2025, 9, 30), "fecha_disponible": date(2025, 11, 29),
          "concepto": "valor_patrimonial_accion", "valor": 1300.0, "unidad": "pesos_por_accion", "base": "separado",
          "fuente": "a"}])
    m = metricas.fact_metrica_fundamental(f)
    val = {(r["metrica"], r["fecha_corte"]): r for r in m.iter_rows(named=True)}
    # 12 meses a sep-2025 = 90 (ene-sep 2025) + 100 (2024) - 60 (ene-sep 2024) = 130
    upa = val[("upa_12m", date(2025, 9, 30))]
    assert upa["valor"] == 130.0 and upa["fecha_disponible"] == date(2025, 11, 29)
    assert val[("roe_12m", date(2025, 9, 30))]["valor"] == 0.1
    assert val[("payout", date(2025, 6, 30))]["valor"] == 0.5  # 50 decretados sobre 100 de 2024
    assert math.isclose(val[("crecimiento_upa_12m", date(2025, 12, 31))]["valor"], 0.2)
