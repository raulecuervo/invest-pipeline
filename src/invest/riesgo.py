"""Métricas de riesgo por acción, a cada fin de mes, en pesos y en dólares (``gold/fact_riesgo``).

Grano: especie × fin de mes × ventana (1, 3 y 5 años) × moneda. Como pide el diseño, riesgo y
correlaciones se miden con retornos **semanales** (último dato de cada semana del índice de retorno
total limpio de ``fact_tecnico_diario``); el drawdown, con los datos diarios. En dólares el índice se
divide por la TRM del día del precio, así que la volatilidad en dólares incluye la del peso.

- ``retorno_anual``: retorno total anualizado de la ventana.
- ``volatilidad_anual``: desviación estándar semanal × √52.
- ``beta`` y ``correlacion`` frente a ``universo_igual`` de ``fact_indice_mercado`` en la misma moneda.
- ``sharpe`` y ``sortino``: contra la tasa libre de riesgo promedio de la ventana: tasa de política
  del Banco de la República en pesos y letra del Tesoro a 3 meses en dólares (``fact_macro``). Si no
  hay tasa para toda la ventana quedan vacíos (``tasa_libre_riesgo`` nula).
- ``drawdown_maximo``: la mayor caída desde un máximo dentro de la ventana (negativa).
- ``var_95`` y ``cvar_95``: retorno semanal del peor 5 % y promedio de esas semanas (históricos).

Solo se calcula para los meses en que la acción negoció en los últimos 30 días. La ventana es de
calendario (las semanas con negocios de los últimos 1, 3 o 5 años) y exige un mínimo de semanas con
datos (``MINIMO_SEMANAS``): una acción que negocia poco no completa la ventana y no tiene fila. La
beta necesita al menos 26 semanas en que negociaron la acción y el índice.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl

from invest import storage
from invest.factores import fines_de_mes

VENTANAS = {"1a": 52, "3a": 156, "5a": 260}
MINIMO_SEMANAS = {"1a": 40, "3a": 110, "5a": 180}
TASA_LIBRE = {"COP": "banrep_59", "USD": "fred_dgs3mo"}


def _semanal(diario: pl.DataFrame, columna: str) -> pl.DataFrame:
    """Último valor de cada semana (lunes a domingo) por especie."""
    return (diario.sort("fecha").with_columns(pl.col("fecha").dt.truncate("1w").alias("semana"))
            .group_by(["especie", "semana"], maintain_order=True).agg(pl.col("fecha").last(), pl.col(columna).last()))


def _tasa_promedio(tasas: pl.DataFrame | None, desde: date, hasta: date) -> float | None:
    if tasas is None or tasas.is_empty():
        return None
    v = tasas.filter((pl.col("fecha") >= desde) & (pl.col("fecha") <= hasta))
    if v.is_empty() or v.get_column("fecha").min() > desde + timedelta(days=45):
        return None  # la serie no cubre la ventana
    return float(v.get_column("valor").mean()) / 100


def _metricas(x: np.ndarray, xb: np.ndarray | None, diario: np.ndarray, rf: float | None, anios: float) -> dict:
    r = x[1:] / x[:-1] - 1
    n = len(r)
    anual = (x[-1] / x[0]) ** (1 / anios) - 1  # en años calendario, aunque falten semanas sin negocio
    vol = float(np.std(r, ddof=1) * math.sqrt(52))
    salida = {"semanas": n, "retorno_anual": anual, "volatilidad_anual": vol}
    if xb is not None:
        rb = xb[1:] / xb[:-1] - 1
        ok = np.isfinite(rb) & np.isfinite(r)
        if ok.sum() >= 26:
            ra, rb = r[ok], rb[ok]
            var_b, var_a = np.var(rb, ddof=1), np.var(ra, ddof=1)
            if var_b > 0:
                cov = np.cov(ra, rb, ddof=1)[0, 1]
                salida["beta"] = float(cov / var_b)
                salida["correlacion"] = float(cov / math.sqrt(var_b * var_a)) if var_a > 0 else None
    if rf is not None:
        salida["tasa_libre_riesgo"] = rf
        salida["sharpe"] = (anual - rf) / vol if vol > 0 else None
        semanal_rf = (1 + rf) ** (1 / 52) - 1
        abajo = np.minimum(r - semanal_rf, 0)
        dd = math.sqrt(np.mean(abajo ** 2)) * math.sqrt(52)
        salida["sortino"] = (anual - rf) / dd if dd > 0 else None
    pico = np.maximum.accumulate(diario)
    salida["drawdown_maximo"] = float(np.min(diario / pico - 1))
    var = float(np.quantile(r, 0.05))
    salida["var_95"] = var
    salida["cvar_95"] = float(r[r <= var].mean())
    return salida


def fact_riesgo() -> pl.DataFrame | None:
    tecnico = storage.leer_tabla("gold", "fact_tecnico_diario")
    indices = storage.leer_tabla("gold", "fact_indice_mercado")
    macro = storage.leer_tabla("gold", "fact_macro")
    if tecnico is None or tecnico.is_empty():
        return None
    from invest.mercado import _trm

    diario = tecnico.select("fecha", "especie", pl.col("indice_rt_limpio").alias("x")).drop_nulls().sort("fecha")
    trm = _trm()
    diario = diario.join_asof(trm, left_on="fecha", right_on="fecha_mercado", strategy="backward",
                              check_sortedness=False, tolerance="7d").with_columns((pl.col("x") / pl.col("trm")).alias("x_usd"))
    ref = {}
    if indices is not None:
        for moneda in ("COP", "USD"):
            b = indices.filter((pl.col("indice_id") == "universo_igual") & (pl.col("moneda") == moneda)) \
                .select(pl.lit("_ref").alias("especie"), "fecha", pl.col("valor").alias("v"))
            ref[moneda] = _semanal(b, "v").select("semana", pl.col("v").alias("xb"))
    tasas = {m: (macro.filter(pl.col("serie_id") == s).select("fecha", "valor") if macro is not None else None)
             for m, s in TASA_LIBRE.items()}
    meses = fines_de_mes(diario.get_column("fecha").min(), diario.get_column("fecha").max())
    ultimo = diario.get_column("fecha").max()
    if meses and meses[-1] < ultimo:
        meses.append(ultimo)  # el mes en curso, a la última sesión con datos
    filas = []
    for moneda, col in (("COP", "x"), ("USD", "x_usd")):
        semanas = _semanal(diario.filter(pl.col(col).is_not_null()), col)
        if moneda in ref:
            semanas = semanas.join(ref[moneda], on="semana", how="left")
        else:
            semanas = semanas.with_columns(pl.lit(None, pl.Float64).alias("xb"))
        dia = diario.filter(pl.col(col).is_not_null())
        for (especie,), g in semanas.group_by(["especie"]):
            g = g.sort("fecha")
            f = g.get_column("fecha").to_numpy()
            xs, xbs = g.get_column(col).to_numpy(), g.get_column("xb").to_numpy().astype(float)
            d = dia.filter(pl.col("especie") == especie).sort("fecha")
            fd, xd = d.get_column("fecha").to_numpy(), d.get_column(col).to_numpy()
            for m in meses:
                m64 = np.datetime64(m)
                fin = np.searchsorted(f, m64, side="right")
                if fin == 0 or f[fin - 1] < m64 - np.timedelta64(30, "D"):
                    continue  # no negoció en el último mes
                for ventana, semanas_v in VENTANAS.items():
                    # Ventana en calendario: semanas con datos desde hace 1, 3 o 5 años (una semana
                    # antes, para tener el punto de partida)
                    ini = int(np.searchsorted(f, m64 - np.timedelta64(semanas_v * 7 + 7, "D"), side="left"))
                    if fin - ini - 1 < MINIMO_SEMANAS[ventana]:
                        continue
                    desde = f[ini]
                    di, df_ = np.searchsorted(fd, desde, side="left"), np.searchsorted(fd, m64, side="right")
                    desde_d = date.fromisoformat(str(desde)[:10])
                    rf = _tasa_promedio(tasas.get(moneda), desde_d, m)
                    anios = float((f[fin - 1] - f[ini]) / np.timedelta64(1, "D")) / 365.25
                    met = _metricas(xs[ini:fin], xbs[ini:fin], xd[di:df_], rf, anios)
                    filas.append({"fecha": m, "especie": especie, "ventana": ventana, "moneda": moneda,
                                  "desde": desde_d, **met})
    if not filas:
        return None
    esquema = {"fecha": pl.Date, "especie": pl.Utf8, "ventana": pl.Utf8, "moneda": pl.Utf8, "desde": pl.Date,
               "semanas": pl.Int64, "retorno_anual": pl.Float64, "volatilidad_anual": pl.Float64,
               "beta": pl.Float64, "correlacion": pl.Float64, "tasa_libre_riesgo": pl.Float64,
               "sharpe": pl.Float64, "sortino": pl.Float64, "drawdown_maximo": pl.Float64,
               "var_95": pl.Float64, "cvar_95": pl.Float64}
    return pl.DataFrame(filas, schema=esquema).sort(["especie", "fecha", "ventana", "moneda"])


def construir() -> dict:
    df = fact_riesgo()
    if df is None:
        return {}
    storage.escribir_por_anio("gold", "fact_riesgo", df, "fecha")
    return {"filas": df.height, "especies": df.get_column("especie").n_unique(),
            "con_sharpe": df.filter(pl.col("sharpe").is_not_null()).height}
