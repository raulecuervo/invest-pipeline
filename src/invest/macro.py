"""Macro y mercado en gold: catálogo de indicadores y una sola tabla larga de valores.

- ``gold/dims/dim_indicador_macro``: una fila por serie (config/indicadores_macro.yaml), con nombre,
  nombre corto, país, categoría, unidad, frecuencia y fuente.
- ``gold/fact_macro``: fecha, serie_id, valor y ``fecha_publicacion`` (desde cuándo se conocía), de
  silver/fact_serie_macro (DANE, FRED, BanRep, índices de la Bolsa) y la TRM (silver/fact_fx_diaria,
  publicada la tarde de su ``fecha_mercado``). Incluye series derivadas: inflación anual de Colombia y
  de EE. UU. y tasa de política real.

Cuando una fuente no trae la fecha de publicación (FRED, BanRep, Bolsa), se usa la fecha del dato:
son series diarias que se publican el mismo día o el siguiente. Para un backtest mensual eso no
adelanta información; para uno diario, la columna ``publicacion_estimada`` lo advierte.
"""

from __future__ import annotations

import polars as pl

from invest import storage
from invest.config import cargar_yaml

ESQUEMA = {"fecha": pl.Date, "serie_id": pl.Utf8, "valor": pl.Float64, "fecha_publicacion": pl.Date,
           "publicacion_estimada": pl.Boolean, "fuente_id": pl.Utf8}


def _config() -> dict:
    return cargar_yaml("indicadores_macro.yaml")


def _origen() -> pl.DataFrame:
    partes = []
    serie = storage.leer_tabla("silver", "fact_serie_macro")
    if serie is not None and not serie.is_empty():
        partes.append(serie.select(
            "fecha", "serie_id", "valor",
            pl.coalesce("fecha_publicacion", "fecha").alias("fecha_publicacion"),
            (pl.col("publicacion_estimada").fill_null(False) | pl.col("fecha_publicacion").is_null())
            .alias("publicacion_estimada"),
            "fuente_id"))
    fx = storage.leer_tabla("silver", "fact_fx_diaria")
    if fx is not None and not fx.is_empty():
        partes.append(fx.filter((pl.col("moneda_origen") == "USD") & (pl.col("moneda_destino") == "COP")).select(
            "fecha", pl.lit("trm_usd_cop").alias("serie_id"), pl.col("tasa").alias("valor"),
            pl.coalesce("fecha_mercado", "fecha").alias("fecha_publicacion"),
            pl.col("fecha_mercado").is_null().alias("publicacion_estimada"), "fuente_id"))
    if not partes:
        return pl.DataFrame(schema=ESQUEMA)
    return (pl.concat([p.cast(ESQUEMA) for p in partes]).drop_nulls(["fecha", "valor"])
            .unique(subset=["fecha", "serie_id"], keep="last"))


def _variacion_12m(df: pl.DataFrame, base: str, nuevo: str) -> pl.DataFrame:
    s = df.filter(pl.col("serie_id") == base).sort("fecha")
    if s.is_empty():
        return pl.DataFrame(schema=ESQUEMA)
    anterior = s.select((pl.col("fecha").dt.offset_by("12mo")).alias("fecha"), pl.col("valor").alias("_v12"))
    return (s.join(anterior, on="fecha", how="inner")
            .with_columns(((pl.col("valor") / pl.col("_v12") - 1) * 100).alias("valor"),
                          pl.lit(nuevo).alias("serie_id"), pl.lit("derivado").alias("fuente_id"))
            .select(list(ESQUEMA)))


def _tasa_real(df: pl.DataFrame, inflacion: pl.DataFrame) -> pl.DataFrame:
    """Tasa de política vigente al cierre de cada mes menos la inflación anual de ese mes."""
    tpm = df.filter(pl.col("serie_id") == "banrep_59").sort("fecha")
    if tpm.is_empty() or inflacion.is_empty():
        return pl.DataFrame(schema=ESQUEMA)
    meses = inflacion.select("fecha", pl.col("valor").alias("_inf"), "fecha_publicacion", "publicacion_estimada") \
        .with_columns(pl.col("fecha").dt.month_end().alias("_fin")).sort("_fin")
    return (meses.join_asof(tpm.select(pl.col("fecha").alias("_ft"), pl.col("valor").alias("_tpm")),
                            left_on="_fin", right_on="_ft", strategy="backward", check_sortedness=False)
            .filter(pl.col("_tpm").is_not_null())
            .with_columns((pl.col("_tpm") - pl.col("_inf")).alias("valor"), pl.lit("tasa_real_banrep").alias("serie_id"),
                          pl.lit("derivado").alias("fuente_id"))
            .select(list(ESQUEMA)))


def fact_macro() -> pl.DataFrame:
    df = _origen()
    inf_co = _variacion_12m(df, "ipc_co", "inflacion_co_12m")
    inf_us = _variacion_12m(df, "fred_cpiaucsl", "inflacion_us_12m")
    return pl.concat([df, inf_co, inf_us, _tasa_real(df, inf_co)]).sort(["serie_id", "fecha"])


def dim_indicador_macro(valores: pl.DataFrame) -> pl.DataFrame:
    cfg = _config()
    filas = []
    resumen = (valores.group_by("serie_id").agg(pl.col("fecha").min().alias("primer_dato"),
                                                 pl.col("fecha").max().alias("ultimo_dato"),
                                                 pl.col("fuente_id").first(), pl.len().alias("observaciones"))
               if not valores.is_empty() else pl.DataFrame(schema={"serie_id": pl.Utf8}))
    meta = {r["serie_id"]: r for r in resumen.iter_rows(named=True)}
    definidas = {**cfg.get("indicadores", {}), **cfg.get("derivados", {})}
    for serie_id in sorted(set(definidas) | set(meta)):
        d, m = definidas.get(serie_id, {}), meta.get(serie_id, {})
        filas.append({
            "serie_id": serie_id, "nombre": d.get("nombre", serie_id), "corto": d.get("corto", serie_id),
            "pais_id": d.get("pais_id"), "categoria": d.get("categoria", "sin clasificar"),
            "unidad": d.get("unidad"), "frecuencia": d.get("frecuencia"),
            "derivada_de": d.get("base"), "fuente_id": m.get("fuente_id"),
            "primer_dato": m.get("primer_dato"), "ultimo_dato": m.get("ultimo_dato"),
            "observaciones": m.get("observaciones", 0),
        })
    return pl.DataFrame(filas, infer_schema_length=None)


def construir() -> dict:
    valores = fact_macro()
    if valores.is_empty():
        return {}
    storage.escribir_por_anio("gold", "fact_macro", valores, "fecha")
    dim = dim_indicador_macro(valores)
    storage.escribir_completa("gold", "dim_indicador_macro", dim)
    return {"series": dim.filter(pl.col("observaciones") > 0).height, "filas": valores.height,
            "sin_clasificar": dim.filter(pl.col("categoria") == "sin clasificar").height}
