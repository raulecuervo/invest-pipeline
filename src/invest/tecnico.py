"""Índices de mercado propios e indicadores técnicos diarios por acción.

- ``gold/fact_indice_mercado``: índices de retorno total del universo líquido, calculados con los
  mismos datos de las acciones, para usarlos como referencia (beta, fuerza relativa) mientras el
  MSCI COLCAP solo tenga historia desde que empezó su lectura manual (nov-2026):
  ``universo_igual`` (pesos iguales) y ``universo_cap`` (ponderado por la capitalización del día
  anterior, solo con acciones que tengan acciones en circulación conocidas). Ambos en pesos y en
  dólares (``moneda``). Un día entra en el índice solo si al menos ``minimo_acciones`` acciones
  líquidas negociaron; los saltos atípicos cuentan como cero.
- ``gold/fact_tecnico_diario``: por especie y día con negocios, sobre el índice de retorno total
  (no sobre el precio, para que un dividendo no parezca una caída): medias de 50 y 200 sesiones con
  negocio y la distancia a ellas, RSI de 14 (Wilder), MACD (12, 26, 9), %B de Bollinger (20, 2),
  caída desde el máximo de 52 semanas, retornos de 1, 3, 6 y 12 meses, volatilidad de 60 sesiones y
  fuerza relativa de 12 meses frente a ``universo_igual``. ``tecnico_valido`` es falso cuando la
  acción no es líquida ese día: sus medias se calculan sobre negocios esporádicos y el diseño pide
  mostrarlas como N/A.

Las ventanas cuentan sesiones con negocios de cada acción, no días calendario; en las acciones
líquidas es lo mismo.
"""

from __future__ import annotations

import math

import polars as pl

from invest import storage

SESIONES_ANIO = 252
MINIMO_ACCIONES = 5


def _trm() -> pl.DataFrame:
    from invest.mercado import _trm as trm

    return trm()


def _a_dolares(df: pl.DataFrame, columna_retorno: str) -> pl.DataFrame:
    """Agrega ``<columna>_usd``: el retorno en dólares con la TRM del día del precio y del anterior."""
    trm = _trm()
    if trm.is_empty():
        return df.with_columns(pl.lit(None, pl.Float64).alias(f"{columna_retorno}_usd"))
    fechas = df.select("fecha").unique().sort("fecha").join_asof(
        trm, left_on="fecha", right_on="fecha_mercado", strategy="backward", check_sortedness=False,
        tolerance="7d").select("fecha", "trm")
    fechas = fechas.with_columns(pl.col("trm").shift(1).alias("_trm_ant"))
    return df.join(fechas, on="fecha", how="left").with_columns(
        ((1 + pl.col(columna_retorno)) * pl.col("_trm_ant") / pl.col("trm") - 1).alias(f"{columna_retorno}_usd")
    ).drop("trm", "_trm_ant")


def fact_indice_mercado() -> pl.DataFrame | None:
    precios = storage.leer_tabla("gold", "fact_precio_diario")
    liquidez = storage.leer_tabla("gold", "fact_liquidez_diaria")
    if precios is None or liquidez is None or precios.is_empty():
        return None
    p = (precios.filter(pl.col("retorno_total").is_not_null())
         .join(liquidez.select("fecha", "especie", "liquida"), on=["fecha", "especie"], how="inner")
         .filter(pl.col("liquida").fill_null(False))
         .with_columns(pl.when(pl.col("salto_atipico").fill_null(False)).then(0.0)
                       .otherwise(pl.col("retorno_total")).alias("r"))
         .sort(["especie", "fecha"])
         .with_columns(pl.col("capitalizacion").shift(1).over("especie").alias("_cap_ant")))
    igual = p.group_by("fecha").agg(pl.col("r").mean().alias("retorno"), pl.len().alias("acciones"))
    cap = (p.filter(pl.col("_cap_ant").is_not_null())
           .group_by("fecha").agg(((pl.col("r") * pl.col("_cap_ant")).sum() / pl.col("_cap_ant").sum()).alias("retorno"),
                                  pl.len().alias("acciones")))
    partes = []
    for indice_id, d in (("universo_igual", igual), ("universo_cap", cap)):
        d = d.filter(pl.col("acciones") >= MINIMO_ACCIONES).sort("fecha")
        if d.is_empty():
            continue
        d = _a_dolares(d, "retorno")
        for moneda, col in (("COP", "retorno"), ("USD", "retorno_usd")):
            serie = d.select("fecha", pl.col(col).fill_null(0.0).alias("retorno"), "acciones").with_columns(
                (100 * (1 + pl.col("retorno")).cum_prod()).alias("valor"),
                pl.lit(indice_id).alias("indice_id"), pl.lit(moneda).alias("moneda"))
            partes.append(serie)
    if not partes:
        return None
    return pl.concat(partes).select("fecha", "indice_id", "moneda", "valor", "retorno", "acciones") \
        .sort(["indice_id", "moneda", "fecha"])


def _rsi(cambio: pl.Expr, n: int = 14) -> pl.Expr:
    alfa = 1 / n
    subida = cambio.clip(lower_bound=0).ewm_mean(alpha=alfa, adjust=False)
    bajada = (-cambio).clip(lower_bound=0).ewm_mean(alpha=alfa, adjust=False)
    return 100 - 100 / (1 + subida / bajada)


def fact_tecnico_diario(indices: pl.DataFrame | None = None) -> pl.DataFrame | None:
    precios = storage.leer_tabla("gold", "fact_precio_diario")
    liquidez = storage.leer_tabla("gold", "fact_liquidez_diaria")
    if precios is None or precios.is_empty():
        return None
    p = (precios.filter(pl.col("retorno_total").is_not_null())
         .with_columns(pl.when(pl.col("salto_atipico").fill_null(False)).then(0.0)
                       .otherwise(pl.col("retorno_total")).alias("r"))
         .sort(["especie", "fecha"])
         .with_columns((100 * (1 + pl.col("r")).cum_prod()).over("especie").alias("x"))
         .select("fecha", "especie", "r", "x"))
    o = pl.col("x")
    sobre = lambda e: e.over("especie")  # noqa: E731
    t = p.with_columns(
        sobre(o.rolling_mean(50)).alias("_sma50"),
        sobre(o.rolling_mean(200)).alias("_sma200"),
        sobre(o.rolling_mean(20)).alias("_bb_m"),
        sobre(o.rolling_std(20)).alias("_bb_s"),
        sobre(o.rolling_max(SESIONES_ANIO, min_samples=20)).alias("_max52"),
        sobre(o.rolling_min(SESIONES_ANIO, min_samples=20)).alias("_min52"),
        sobre(_rsi(o.diff())).alias("rsi_14"),
        sobre(o.ewm_mean(span=12, adjust=False) - o.ewm_mean(span=26, adjust=False)).alias("_macd"),
        sobre(pl.col("r").rolling_std(60)).alias("_vol60"),
        sobre(pl.int_range(1, pl.len() + 1)).alias("_n"),
        *[sobre(o / o.shift(k) - 1).alias(f"retorno_{nombre}")
          for nombre, k in (("1m", 21), ("3m", 63), ("6m", 126), ("12m", SESIONES_ANIO))],
    ).with_columns(
        sobre(pl.col("_macd").ewm_mean(span=9, adjust=False)).alias("_senal"),
    )
    t = t.with_columns(
        (o / pl.col("_sma50") - 1).alias("distancia_sma50"),
        (o / pl.col("_sma200") - 1).alias("distancia_sma200"),
        (pl.col("_sma50") > pl.col("_sma200")).alias("tendencia_alcista"),
        ((o - (pl.col("_bb_m") - 2 * pl.col("_bb_s"))) / (4 * pl.col("_bb_s"))).alias("bollinger_pct_b"),
        (o / pl.col("_max52") - 1).alias("caida_desde_max_52s"),
        (o / pl.col("_min52") - 1).alias("subida_desde_min_52s"),
        (pl.col("_macd") / pl.col("_sma50")).alias("macd"),
        ((pl.col("_macd") - pl.col("_senal")) / pl.col("_sma50")).alias("macd_histograma"),
        (pl.col("_vol60") * math.sqrt(SESIONES_ANIO)).alias("volatilidad_60"),
        pl.col("_n").alias("sesiones_historia"),
    )
    # Medias sin historia suficiente quedan vacías (rolling_mean ya las deja nulas)
    t = t.with_columns(
        pl.when(pl.col("_n") < 26).then(None).otherwise(pl.col("macd")).alias("macd"),
        pl.when(pl.col("_n") < 35).then(None).otherwise(pl.col("macd_histograma")).alias("macd_histograma"),
        pl.when(pl.col("_n") < 15).then(None).otherwise(pl.col("rsi_14")).alias("rsi_14"),
    )
    # Fuerza relativa de 12 meses frente al universo con pesos iguales (mismo día)
    indices = indices if indices is not None else fact_indice_mercado()
    if indices is not None and not indices.is_empty():
        ref = (indices.filter((pl.col("indice_id") == "universo_igual") & (pl.col("moneda") == "COP"))
               .sort("fecha").with_columns((pl.col("valor") / pl.col("valor").shift(SESIONES_ANIO) - 1)
                                           .alias("_ref12")).select("fecha", "_ref12"))
        t = t.join(ref, on="fecha", how="left").with_columns(
            ((1 + pl.col("retorno_12m")) / (1 + pl.col("_ref12")) - 1).alias("fuerza_relativa_12m"))
    else:
        t = t.with_columns(pl.lit(None, pl.Float64).alias("fuerza_relativa_12m"))
    if liquidez is not None:
        t = t.join(liquidez.select("fecha", "especie", "liquida"), on=["fecha", "especie"], how="left")
    else:
        t = t.with_columns(pl.lit(None, pl.Boolean).alias("liquida"))
    t = t.with_columns(pl.col("liquida").fill_null(False).alias("tecnico_valido"))
    return t.select(
        "fecha", "especie", pl.col("x").alias("indice_rt_limpio"), "distancia_sma50", "distancia_sma200",
        "tendencia_alcista", "rsi_14", "macd", "macd_histograma", "bollinger_pct_b", "caida_desde_max_52s",
        "subida_desde_min_52s", "retorno_1m", "retorno_3m", "retorno_6m", "retorno_12m", "volatilidad_60",
        "fuerza_relativa_12m", "sesiones_historia", "tecnico_valido",
    ).sort(["especie", "fecha"])


def construir() -> dict:
    resumen: dict = {}
    indices = fact_indice_mercado()
    if indices is not None:
        storage.escribir_por_anio("gold", "fact_indice_mercado", indices, "fecha")
        resumen["indices_dias"] = indices.filter((pl.col("indice_id") == "universo_igual")
                                                 & (pl.col("moneda") == "COP")).height
    tecnico = fact_tecnico_diario(indices)
    if tecnico is not None:
        storage.escribir_por_anio("gold", "fact_tecnico_diario", tecnico, "fecha")
        resumen["filas_tecnico"] = tecnico.height
    return resumen
