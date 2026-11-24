import random
from datetime import date

import polars as pl

from invest import factores, ficha, storage, tablero


def _factores_sinteticos(meses: list[date], n: int = 12, semilla: int = 3) -> pl.DataFrame:
    """Dos factores con signos opuestos (a: más es mejor; b: menos es mejor) y uno de contexto."""
    rnd = random.Random(semilla)
    filas = []
    for m in meses:
        for k in range(n):
            a, b = rnd.gauss(0, 1), rnd.gauss(0, 1)
            ret = 0.01 * a - 0.01 * b + rnd.gauss(0, 0.03)
            for nombre, v in (("momentum_12_1", a), ("rendimiento_dividendo_12m", b), ("reversion_1m", rnd.gauss(0, .05))):
                filas.append({"fecha": m, "especie": f"e{k:02d}", "retorno_siguiente": ret, "factor": nombre, "valor": v})
    df = pl.DataFrame(filas)
    return df.with_columns(
        ((pl.col("valor").rank("average").over(["fecha", "factor"]) - 0.5) / pl.len().over(["fecha", "factor"])).alias("percentil"),
        pl.len().over(["fecha", "factor"]).alias("acciones_mes"))


def _cfg():
    cfg = factores.config()
    cfg = {**cfg, "factores": {**cfg["factores"], "rendimiento_dividendo_12m": {"signo": -1}}}
    return cfg


def test_puntaje_y_tercio_iguales_a_la_validacion():
    cfg = _cfg()
    meses = factores.fines_de_mes(date(2015, 1, 1), date(2019, 12, 31))
    f = _factores_sinteticos(meses)
    validados = ["momentum_12_1", "rendimiento_dividendo_12m"]
    p = ficha.puntajes(f, validados, cfg)
    # b tiene signo -1: el percentil se invierte antes de promediar
    fila = p.filter((pl.col("fecha") == meses[0]) & (pl.col("posicion") == 1)).row(0, named=True)
    origen = f.filter((pl.col("fecha") == meses[0]) & (pl.col("especie") == fila["especie"]))
    pa = origen.filter(pl.col("factor") == "momentum_12_1").get_column("percentil")[0]
    pb = origen.filter(pl.col("factor") == "rendimiento_dividendo_12m").get_column("percentil")[0]
    assert abs(fila["puntaje"] - (pa + 1 - pb) / 2) < 1e-12
    assert p.filter(pl.col("fecha") == meses[0]).get_column("tercio").value_counts().sort("tercio").get_column("count").to_list() == [4, 4, 4]

    # La historia del tercio superior reproduce el exceso que la validación reporta para el combinado
    costo = 0.005
    h = ficha.historia_tercio(p, meses[0], costo, 8)
    comb = p.select("fecha", "especie", pl.col("puntaje").alias("valor"), "retorno_siguiente")
    m = factores._metricas(comb, 1, 8, 3, 3, costo)
    assert abs(h.get_column("exceso").mean() - m["exceso_superior"]) < 1e-12
    assert abs(h.get_column("exceso_neto").mean() - m["exceso_superior_neto"]) < 1e-12
    assert abs(h.get_column("rotacion")[1:].mean() - m["rotacion_superior"]) < 1e-12


def test_ficha_y_tablero(carpeta_datos, monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(ficha, "config_factores", lambda: cfg)
    monkeypatch.setattr(tablero, "config_factores", lambda: cfg)
    meses = factores.fines_de_mes(date(2024, 1, 1), date(2025, 6, 30))
    f = _factores_sinteticos(meses)
    storage.escribir_completa("gold", "fact_factor_mensual", f)
    storage.escribir_completa("gold", "fact_validacion_factor", pl.DataFrame([
        {"factor": "momentum_12_1", "veredicto": "validado", "meses": 18, "ic_medio": 0.06},
        {"factor": "rendimiento_dividendo_12m", "veredicto": "validado", "meses": 18, "ic_medio": 0.05},
        {"factor": "reversion_1m", "veredicto": "no_validado", "meses": 18, "ic_medio": 0.0},
    ]))
    especies = [f"e{k:02d}" for k in range(12)] + ["zz_iliquida"]
    (carpeta_datos / "gold" / "dims").mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "especie": especies,
        "nombre": [f"AO EMPRESA {k} S.A." for k in range(12)] + ["AO ILIQUIDA </script> S.A."],
        "emisor_id": [f"CO-E{k:02d}" for k in range(13)], "clase_accion": ["ordinaria"] * 13,
        "sector_sfc": [None] * 13, "activa": [True] * 13,
    }).write_parquet(carpeta_datos / "gold" / "dims" / "dim_activo.parquet")
    ultimo = meses[-1]
    ret, liq = [], []
    for e in especies:
        for d in factores.sesiones_entre(date(2023, 1, 1), ultimo):
            if e == "zz_iliquida" and d.day != 15:
                continue
            ret.append({"fecha": d, "especie": e, "precio_promedio": 1000.0, "salto_atipico": False})
            liq.append({"fecha": d, "especie": e, "monto_medio_60": 1e9, "fraccion_dias_60": 1.0,
                        "liquida": e != "zz_iliquida"})
            if e == "zz_iliquida":
                liq[-1]["fraccion_dias_60"] = 0.05
    storage.escribir_completa("gold", "fact_retorno_diario", pl.DataFrame(ret))
    storage.escribir_completa("gold", "fact_liquidez_diaria", pl.DataFrame(liq))
    storage.escribir_completa("gold", "fact_fundamental", pl.DataFrame([
        {"emisor_id": "CO-E00", "fecha_corte": date(2024, 12, 31), "fecha_disponible": date(2025, 4, 10),
         "concepto": "utilidad_accion", "valor": 100.0, "unidad": "pesos_por_accion"},
        {"emisor_id": "CO-E00", "fecha_corte": date(2025, 3, 31), "fecha_disponible": date(2025, 5, 30),
         "concepto": "utilidad_accion", "valor": 30.0, "unidad": "pesos_por_accion"},
        {"emisor_id": "CO-E00", "fecha_corte": date(2025, 3, 31), "fecha_disponible": date(2025, 5, 30),
         "concepto": "valor_patrimonial_accion", "valor": 800.0, "unidad": "pesos_por_accion"},
        # Publicado después del último fin de mes: no puede usarse
        {"emisor_id": "CO-E00", "fecha_corte": date(2025, 6, 30), "fecha_disponible": date(2025, 8, 29),
         "concepto": "valor_patrimonial_accion", "valor": 5.0, "unidad": "pesos_por_accion"},
    ]))

    tablas, resumen = ficha.construir()
    assert resumen["con_puntaje"] == 12 and resumen["fuera_del_universo"] == 1
    t = tablas["ficha"]
    e0 = t.filter(pl.col("especie") == "e00").row(0, named=True)
    # Utilidad del último cierre anual (no la del trimestre) y valor patrimonial ya publicado
    assert e0["utilidad_accion_anual"] == 100.0 and e0["precio_utilidad"] == 10.0
    assert e0["valor_patrimonial_accion"] == 800.0 and e0["precio_valor_libros"] == 1.25
    z = t.filter(pl.col("especie") == "zz_iliquida").row(0, named=True)
    assert not z["en_universo"] and z["motivo_fuera"] and "mitad" in z["alertas"]
    # Meses seguidos en el tercio superior: consistente con la tabla mensual
    p = tablas["puntajes"]
    for r in t.filter(pl.col("con_puntaje")).iter_rows(named=True):
        hist = p.filter(pl.col("especie") == r["especie"]).sort("fecha", descending=True).get_column("tercio").to_list()
        n = next((i for i, x in enumerate(hist) if x != "superior"), len(hist))
        assert r["meses_seguidos_superior"] == n

    destino = tablero.escribir()
    html = destino.read_text(encoding="utf-8")
    assert "/*__DATOS__*/" not in html and f'"fecha": "{ultimo.isoformat()}"' in html
    # El nombre con </script> no puede cerrar el bloque de datos
    assert html.count("</script>") == 1


def test_nombre_corto():
    assert tablero.nombre_corto("cot13paad031", "ADP GRUPO DE INVERSIONES SURAMERICANA S.A.", "preferencial",
                                {"cot13": "Grupo Sura"}) == "Grupo Sura (pref.)"
    assert tablero.nombre_corto("cox99paao001", "AO PEC NUEVA EMPRESA S.A. E.S.P. (ANTES VIEJA)", "ordinaria", {}) == "Nueva Empresa"
