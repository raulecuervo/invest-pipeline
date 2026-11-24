"""Liquidez diaria de cada acción: monto medio negociado y fracción de ruedas con negocios.

La SFC publica una fila por acción y día aunque no haya negocios (valor transado 0, precio del
último negocio). Sobre esa serie completa, para cada día:

    monto_medio_20     promedio del valor transado en las últimas 20 ruedas (incluye los días en 0)
    fraccion_dias_20   fracción de esas 20 ruedas con algún negocio
    (igual con la ventana larga de 60 ruedas)
    liquida            monto_medio_20 ≥ mínimo y fraccion_dias_20 ≥ mínima (config/convenciones.yaml)

Las primeras ruedas de cada acción, sin ventana completa, quedan en nulo. Va a
gold/fact_liquidez_diaria, por año y solo lo que cambia.
"""

from __future__ import annotations

import polars as pl

from invest import storage
from invest import sucesiones
from invest.config import convenciones


def construir() -> tuple[pl.DataFrame | None, dict]:
    precios = sucesiones.precios_sfc()
    if precios is None or precios.is_empty():
        return None, {}
    cfg = convenciones()["liquidez"]
    corta, larga = int(cfg["ventana_corta"]), int(cfg["ventana_larga"])
    monto_min, frac_min = float(cfg["monto_medio_minimo_cop"]), float(cfg["fraccion_dias_minima"])
    df = (
        precios.select("fecha", "especie", "valor_transado").sort(["especie", "fecha"])
        .with_columns(pl.col("valor_transado").fill_null(0.0), (pl.col("valor_transado") > 0).cast(pl.Float64).alias("_neg"))
        .with_columns(
            pl.col("valor_transado").rolling_mean(corta).over("especie").alias("monto_medio_20"),
            pl.col("_neg").rolling_mean(corta).over("especie").alias("fraccion_dias_20"),
            pl.col("valor_transado").rolling_mean(larga).over("especie").alias("monto_medio_60"),
            pl.col("_neg").rolling_mean(larga).over("especie").alias("fraccion_dias_60"),
        )
        .with_columns(
            ((pl.col("monto_medio_20") >= monto_min) & (pl.col("fraccion_dias_20") >= frac_min)).alias("liquida"),
        )
        .select("fecha", "especie", "valor_transado", "monto_medio_20", "fraccion_dias_20", "monto_medio_60",
                "fraccion_dias_60", "liquida")
    )
    storage.escribir_por_anio("gold", "fact_liquidez_diaria", df, "fecha")
    ultimo = df.filter(pl.col("fecha") == pl.col("fecha").max().over("especie"))
    resumen = {
        "acciones": df.get_column("especie").n_unique(),
        "liquidas_hoy": ultimo.filter(pl.col("liquida").fill_null(False)
                                      & (pl.col("fecha") == df.get_column("fecha").max())).height,
    }
    return df, resumen
