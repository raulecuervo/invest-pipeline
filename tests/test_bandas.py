"""Bandas de valoración (src/invest/bandas.py): as-of, z, percentil, cobertura y quiebres."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from invest import bandas

CFG = {"indicadores": ["p_vl", "p_u"], "ventanas": {"1a": 1, "3a": 3}, "cobertura_minima": 0.6,
       "minimo_observaciones": 5, "dias_precio_reciente": 30, "recortar_al_quiebre": False}


def _dias_habiles(desde: date, hasta: date) -> list[date]:
    return [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)
            if (desde + timedelta(days=i)).weekday() < 5]


def _diaria(fechas: list[date], valores, especie: str = "coa01paao001", indicador: str = "p_vl") -> pl.DataFrame:
    return pl.DataFrame({"fecha": fechas, "especie": [especie] * len(fechas), "emisor_id": ["CO-COA01"] * len(fechas),
                         indicador: [float(v) if v is not None else None for v in valores]},
                        schema_overrides={indicador: pl.Float64})


def _fila(df: pl.DataFrame, fecha: date, ventana: str = "1a", indicador: str = "p_vl") -> dict:
    r = df.filter((pl.col("fecha") == fecha) & (pl.col("ventana") == ventana) & (pl.col("indicador") == indicador))
    assert r.height == 1
    return r.row(0, named=True)


# --------------------------------------------------------------------------- valoración diaria (as-of)
def _metricas(filas: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame([{"emisor_id": e, "fecha_corte": c, "fecha_disponible": d, "base": "separado", "metrica": m,
                          "valor": v, "unidad": u} for e, c, d, m, v, u in filas])


def test_diaria_usa_lo_conocido_cada_dia_y_las_cifras_del_emisor():
    fechas = [date(2025, 4, 1), date(2025, 4, 9), date(2025, 4, 10), date(2025, 6, 2)]
    precios = pl.DataFrame({
        "fecha": fechas * 2, "especie": ["coa01paao001"] * 4 + ["coa01paad002"] * 4, "emisor_id": ["CO-COA01"] * 8,
        "precio": [100.0, 110.0, 120.0, 130.0, 50.0, 55.0, 60.0, 65.0], "trm": [4000.0] * 8})
    metricas = _metricas([
        ("CO-COA01", date(2024, 12, 31), date(2025, 4, 10), "vpa", 200.0, "pesos_por_accion"),
        ("CO-COA01", date(2024, 9, 30), date(2024, 11, 29), "vpa", 100.0, "pesos_por_accion"),
        ("CO-COA01", date(2024, 12, 31), date(2025, 4, 10), "upa_12m", -5.0, "pesos_por_accion"),
        ("CO-COA01", date(2025, 3, 31), date(2025, 5, 30), "upa_12m", 10.0, "pesos_por_accion"),
    ])
    d = bandas.fact_valoracion_diaria(precios, metricas)
    o = d.filter(pl.col("especie") == "coa01paao001").sort("fecha")
    # Hasta el 9-abr se conoce el corte de sep-2024 (100); desde el 10-abr, el de dic-2024 (200)
    assert o.get_column("vpa").to_list() == [100.0, 100.0, 200.0, 200.0]
    assert o.get_column("p_vl").to_list() == [1.0, 1.1, 0.6, 0.65]
    # Utilidad negativa: sin P/U; la positiva llega el 30-may
    assert o.get_column("p_u").to_list() == [None, None, None, 13.0]
    # La preferencial usa las cifras por acción del emisor
    p = d.filter(pl.col("especie") == "coa01paad002").sort("fecha")
    assert p.get_column("p_vl").to_list() == [0.5, 0.55, 0.3, 0.325]


def test_diaria_dolares_a_pesos_y_vpa_no_positivo():
    precios = pl.DataFrame({"fecha": [date(2025, 6, 2), date(2025, 6, 3)], "especie": ["x"] * 2, "emisor_id": ["E"] * 2,
                            "precio": [400.0, 400.0], "trm": [4000.0, 4000.0]})
    metricas = _metricas([("E", date(2025, 3, 31), date(2025, 6, 3), "vpa", 0.2, "usd_por_accion"),
                          ("E", date(2024, 12, 31), date(2025, 4, 1), "vpa", -1.0, "usd_por_accion")])
    d = bandas.fact_valoracion_diaria(precios, metricas).sort("fecha")
    assert d.get_column("p_vl").to_list() == [None, 0.5]  # vpa negativo → nulo; 0,2 USD × 4.000 = 800


# --------------------------------------------------------------------------- z, percentil y cobertura
def test_z_media_desviacion_y_percentil():
    fechas = _dias_habiles(date(2024, 1, 1), date(2024, 12, 31))
    rnd = np.random.default_rng(3)
    valores = list(1 + rnd.normal(0, 0.1, len(fechas)))
    d = bandas.bandas_en_fechas(_diaria(fechas, valores), [fechas[-1]], sesiones=fechas, cfg=CFG)
    r = _fila(d, fechas[-1])
    desde = bandas.restar_anios(fechas[-1], 1)
    x = np.array([v for f, v in zip(fechas, valores) if f >= desde])
    assert r["n_obs"] == len(x) and r["cobertura"] == pytest.approx(1.0)
    assert r["media"] == pytest.approx(x.mean()) and r["desviacion"] == pytest.approx(x.std(ddof=1))
    assert r["z"] == pytest.approx((valores[-1] - x.mean()) / x.std(ddof=1))
    assert r["percentil"] == pytest.approx(((x < valores[-1]).sum() + 0.5) / len(x))
    assert r["actual"] == valores[-1] and r["fecha_desde"] == desde and r["suficiente"]
    assert r["quiebre"] is None and r["aviso"] is None


def test_percentil_extremos_y_signo_de_z():
    fechas = _dias_habiles(date(2024, 1, 1), date(2024, 6, 28))
    subiendo = bandas.bandas_en_fechas(_diaria(fechas, range(1, len(fechas) + 1)), [fechas[-1]], sesiones=fechas, cfg=CFG)
    r = _fila(subiendo, fechas[-1])
    assert r["z"] > 1.5 and r["percentil"] == pytest.approx(1 - 0.5 / r["n_obs"])
    bajando = bandas.bandas_en_fechas(_diaria(fechas, range(len(fechas), 0, -1)), [fechas[-1]], sesiones=fechas, cfg=CFG)
    r = _fila(bajando, fechas[-1])
    assert r["z"] < -1.5 and r["percentil"] == pytest.approx(0.5 / r["n_obs"])


def test_sin_mirar_al_futuro():
    fechas = _dias_habiles(date(2023, 1, 2), date(2024, 12, 31))
    corte = date(2024, 6, 28)
    valores = [1 + 0.001 * i for i in range(len(fechas))]
    base = bandas.bandas_en_fechas(_diaria(fechas, valores), [corte], sesiones=fechas, cfg=CFG)
    # Cambiar todo lo posterior a la fecha evaluada no cambia nada de esa fecha
    otros = [v if f <= corte else 50.0 for f, v in zip(fechas, valores)]
    otra = bandas.bandas_en_fechas(_diaria(fechas, otros), [corte], sesiones=fechas, cfg=CFG)
    assert base.drop("aviso").to_dicts() == otra.drop("aviso").to_dicts()
    # Y una fecha posterior sí lo ve
    despues = bandas.bandas_en_fechas(_diaria(fechas, otros), [fechas[-1]], sesiones=fechas, cfg=CFG)
    assert _fila(despues, fechas[-1])["actual"] == 50.0


def test_cobertura_minima_y_actual_reciente():
    sesiones = _dias_habiles(date(2024, 1, 1), date(2024, 12, 31))
    con_dato = sesiones[::2]  # la mitad de las sesiones
    d = bandas.bandas_en_fechas(_diaria(con_dato, [1 + (i % 7) / 10 for i in range(len(con_dato))]), [sesiones[-1]],
                                sesiones=sesiones, cfg=CFG)
    r = _fila(d, sesiones[-1])
    assert r["cobertura"] == pytest.approx(0.5, abs=0.01)
    assert r["z"] is None and r["percentil"] is None and not r["suficiente"]
    assert r["media"] is not None and "cobertura de 50 %" in r["aviso"]
    # Con 40 % de mínimo sí hay z
    d = bandas.bandas_en_fechas(_diaria(con_dato, [1 + (i % 7) / 10 for i in range(len(con_dato))]), [sesiones[-1]],
                                sesiones=sesiones, cfg={**CFG, "cobertura_minima": 0.4})
    assert _fila(d, sesiones[-1])["z"] is not None
    # Sin negocios en los últimos 30 días no hay "actual" (ni fila)
    viejo = [f for f in sesiones if f <= date(2024, 10, 31)]
    d = bandas.bandas_en_fechas(_diaria(viejo, [1.0 + i / 100 for i in range(len(viejo))]), [sesiones[-1]],
                                sesiones=sesiones, cfg=CFG)
    assert d.is_empty()


def test_indicador_nulo_el_dia_actual_no_toma_un_valor_viejo():
    fechas = _dias_habiles(date(2024, 1, 1), date(2024, 12, 31))
    valores = [10.0 + i % 5 for i in range(len(fechas))]
    valores[-1] = None  # la utilidad pasó a ser negativa: sin P/U hoy
    d = bandas.bandas_en_fechas(_diaria(fechas, valores, indicador="p_u"), [fechas[-1]], sesiones=fechas, cfg=CFG)
    assert d.is_empty()


# --------------------------------------------------------------------------- quiebres
def test_quiebres_de_eventos_y_splits():
    eventos = [{"especie": "a", "fecha_ex": "2025-07-28", "tipo": "escision"},
               {"especie": None, "fecha_ex": "2025-01-01"}]
    splits = pl.DataFrame({"especie": ["b"], "fecha": [date(2022, 11, 23)], "factor_acciones": [3.0]})
    q = bandas.quiebres(eventos, splits)
    assert q.select("especie", "fecha", "tipo").rows() == [("a", date(2025, 7, 28), "escision"),
                                                          ("b", date(2022, 11, 23), "split")]
    assert q.get_column("indicador").null_count() == 2


def test_quiebre_en_la_ventana_aviso_y_recorte():
    fechas = _dias_habiles(date(2023, 1, 2), date(2024, 12, 31))
    valores = [1.0 + (i % 11) / 20 for i in range(len(fechas))]
    q = pl.DataFrame({"especie": ["coa01paao001", "coa01paao001"], "fecha": [date(2024, 7, 29), date(2025, 3, 3)],
                      "tipo": ["escision", "split"], "indicador": [None, None]}, schema_overrides={"indicador": pl.Utf8})
    d = bandas.bandas_en_fechas(_diaria(fechas, valores), [fechas[-1], date(2024, 6, 28)], q, sesiones=fechas, cfg=CFG)
    r = _fila(d, fechas[-1])
    # El último quiebre dentro de la ventana (el de 2025 es posterior: no se conoce)
    assert r["quiebre"] == date(2024, 7, 29) and r["tipo_quiebre"] == "escision" and not r["recortada"]
    assert r["aviso"].startswith("Escisión el 29-jul-2024")
    assert _fila(d, date(2024, 6, 28))["quiebre"] is None  # antes del evento
    # Recortar: la ventana empieza el día del evento y la cobertura se sigue midiendo sobre la ventana completa
    d = bandas.bandas_en_fechas(_diaria(fechas, valores), [fechas[-1]], q, sesiones=fechas,
                                cfg={**CFG, "recortar_al_quiebre": True, "cobertura_minima": 0.3})
    r = _fila(d, fechas[-1])
    assert r["recortada"] and r["fecha_desde"] == date(2024, 7, 29)
    assert r["n_obs"] == len([f for f in fechas if f >= date(2024, 7, 29)])
    assert r["cobertura"] == pytest.approx(r["n_obs"] / r["sesiones"]) and r["cobertura"] < 0.5
    assert "la ventana empieza ese día" in r["aviso"] and r["z"] is not None
    # Un quiebre solo del P/VL no toca el P/U
    q2 = q.with_columns(pl.lit("p_vl").alias("indicador"))
    diaria = _diaria(fechas, valores).with_columns(pl.col("p_vl").alias("p_u"))
    d = bandas.bandas_en_fechas(diaria, [fechas[-1]], q2, sesiones=fechas, cfg=CFG)
    assert _fila(d, fechas[-1])["quiebre"] is not None
    assert _fila(d, fechas[-1], indicador="p_u")["quiebre"] is None


def test_saltos_del_valor_patrimonial_por_accion():
    fechas = [date(2026, 5, 29), date(2026, 6, 1), date(2026, 6, 2), date(2026, 9, 1)]
    diaria = pl.DataFrame({"fecha": fechas, "especie": ["x"] * 4, "vpa": [300.0, 450_000.0, 450_000.0, 200_000.0]})
    s = bandas.saltos_vpa(diaria, 3)
    assert s.rows() == [("x", date(2026, 6, 1), "salto_vpa", "p_vl")]  # 450.000 → 200.000 es menos de 3 veces
    assert bandas.saltos_vpa(diaria, 0).is_empty()


# --------------------------------------------------------------------------- de punta a punta
def test_modelo_y_aplicativo_con_datos_sinteticos(carpeta_datos):
    pytest.importorskip("fastapi")
    import app_datos_util
    from fastapi.testclient import TestClient

    from invest import storage
    from invest.app import servidor

    resumen = app_datos_util.construir()
    # El conjunto sintético no trae todas las tablas del contrato; las de bandas sí deben cumplirlo
    assert not [x for x in resumen["problemas"] if "banda" in x or "quiebre" in x or "valoracion_diaria" in x
                or "z_p_" in x]
    assert resumen["calculo"]["bandas"]["especies_hoy"] == 6
    hoy = storage.leer_tabla("gold", "fact_banda_valoracion")
    assert hoy.get_column("fecha").n_unique() == 1 and hoy.height == 6 * 2 * 4
    # Las fechas de la serie mensual coinciden con las de fact_matriz, que trae el z de 5 años
    matriz = storage.leer_tabla("gold", "fact_matriz")
    assert {"z_p_vl_5a", "quiebre_p_vl_5a"} <= set(matriz.columns)
    mensual = storage.leer_tabla("gold", "fact_banda_valoracion_mensual")
    assert set(mensual.get_column("fecha").unique()) <= set(matriz.get_column("fecha").unique())

    c = TestClient(servidor.app, base_url="http://127.0.0.1:8790")
    t = c.get("/api/valoracion/bandas?indicador=p_vl&ventana=1a").json()
    assert len(t["filas"]) == 6 and t["ventanas"] == ["1a", "3a", "5a", "10a"]
    assert all(f["z"] is not None for f in t["filas"]) and t["filas"][0]["nemotecnico"]
    assert c.get("/api/valoracion/bandas?indicador=roe&ventana=1a").status_code == 400
    assert c.get("/api/valoracion/bandas?indicador=p_vl&ventana=7a").status_code == 400
    f = c.get("/api/ficha/ALFA/bandas?indicador=p_vl&ventana=1a").json()  # nemotécnico → especie
    assert f["tiene"] and f["especie"] == app_datos_util.ESPECIES[0]
    assert f["serie"][0]["fecha"] >= f["banda"]["fecha_desde"] and f["serie"][-1]["fecha"] == f["banda"]["fecha"]
    assert len(f["resumen"]) == 8
    assert c.get("/api/ficha/__no_existe__/bandas").json()["tiene"] is False
