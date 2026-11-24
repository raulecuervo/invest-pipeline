"""Construcción de dimensiones conformadas (gold/dims).

País, moneda y clase de activo son dimensiones: agregar Chile o Perú es activar filas en
config/dimensiones.yaml, no cambiar código.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from invest import (distribuciones, dividendos, dividendos_web, eventos, fundamentales, liquidez, macro, mercado,
                    retornos, splits, storage)
from invest.calendario import es_habil, limites
from invest.config import catalogo_fuentes, dimensiones


def dim_fecha(desde: date, hasta: date) -> pl.DataFrame:
    fechas = pl.date_range(desde, hasta, interval="1d", eager=True).alias("fecha")
    df = pl.DataFrame(fechas)
    return df.with_columns(
        pl.col("fecha").dt.year().alias("anio"),
        pl.col("fecha").dt.quarter().alias("trimestre"),
        pl.col("fecha").dt.month().alias("mes"),
        pl.col("fecha").dt.weekday().alias("dia_semana"),  # 1 = lunes
        (pl.col("fecha").dt.month_end() == pl.col("fecha")).alias("es_fin_mes"),
        pl.col("fecha").dt.week().alias("semana_iso"),
    )


def dim_sesion(mics: list[str], desde: date, hasta: date) -> pl.DataFrame:
    filas = []
    for mic in mics:
        primero, ultimo = limites(mic)
        for d in pl.date_range(desde, hasta, interval="1d", eager=True).to_list():
            filas.append(
                {
                    "fecha": d,
                    "mic": mic,
                    "es_habil": es_habil(d, mic),
                    "calendario_oficial": primero <= d <= ultimo,
                }
            )
    return pl.DataFrame(filas)


def dim_fuente() -> pl.DataFrame:
    columnas = [
        "id", "nombre", "tipo_dato", "url", "acceso", "frecuencia", "recarga_dias",
        "ejecuta_en", "licencia", "redistribuible", "estado", "fase",
    ]
    filas = [{c: f.get(c) for c in columnas} for f in catalogo_fuentes()]
    return pl.DataFrame(filas, infer_schema_length=None).rename({"id": "fuente_id"})


def dim_activo() -> pl.DataFrame | None:
    """Acciones colombianas a partir del índice de la SFC, su ficha y sus precios.

    activo_id es el código de la especie en mayúsculas (p. ej. COC04PAAO008). El nemotécnico de la
    Bolsa (ECOPETROL, PFCIBEST...) se enlaza cuando entre la fuente de la Bolsa de Colombia.
    """
    especies = storage.leer_tabla("silver", "dim_especie_sfc")
    if especies is None or especies.is_empty():
        return None
    ultimo_indice = especies.get_column("fecha_reporte").max()
    base = (
        especies.sort("fecha_reporte")
        .group_by("especie", maintain_order=True)
        .agg(
            pl.col("nombre").last(),
            pl.col("emisor").last(),
            pl.col("clase").last().alias("clase_accion"),
            pl.col("fecha_reporte").min().alias("visto_desde"),
            (pl.col("fecha_reporte").max() == ultimo_indice).alias("en_indice_vigente"),
        )
    )
    fichas = storage.leer_tabla("silver", "fact_indicadores_sfc")
    if fichas is not None and not fichas.is_empty():
        ultima_ficha = (
            fichas.sort("fecha_reporte").group_by("especie").agg(
                pl.col("sector_sfc").drop_nulls().last(),
                pl.col("iba_categoria").drop_nulls().last(),
            )
        )
        base = base.join(ultima_ficha, on="especie", how="left")
    else:
        base = base.with_columns(pl.lit(None, pl.Utf8).alias("sector_sfc"), pl.lit(None, pl.Utf8).alias("iba_categoria"))
    precios = storage.leer_tabla("silver", "fact_precio_sfc")
    if precios is not None and not precios.is_empty():
        rango = precios.group_by("especie").agg(
            pl.col("fecha").min().alias("primera_negociacion"),
            pl.col("fecha").max().alias("ultima_negociacion"),
            pl.len().alias("dias_con_negociacion"),
        )
        base = base.join(rango, on="especie", how="left")
        ultima_global = precios.get_column("fecha").max()
        activa = pl.col("ultima_negociacion") >= pl.lit(ultima_global) - pl.duration(days=45)
    else:
        base = base.with_columns(
            pl.lit(None, pl.Date).alias("primera_negociacion"),
            pl.lit(None, pl.Date).alias("ultima_negociacion"),
            pl.lit(None, pl.UInt32).alias("dias_con_negociacion"),
        )
        activa = pl.lit(None, pl.Boolean)
    # Nemotécnico de la Bolsa de Colombia (config/nemotecnicos.yaml)
    from invest.ingest.bvc import equivalencias

    nemos = {v["especie"]: k for k, v in equivalencias().items() if v.get("especie")}
    base = base.with_columns(pl.col("especie").replace_strict(nemos, default=None).alias("nemotecnico"))
    return base.with_columns(
        pl.col("especie").str.to_uppercase().alias("activo_id"),
        ("CO-" + pl.col("emisor").str.to_uppercase()).alias("emisor_id"),
        pl.lit("ACCION").alias("clase_activo_id"),
        pl.lit("CO").alias("pais_id"),
        pl.lit("COP").alias("moneda_id"),
        pl.lit("XBOG").alias("mic"),
        activa.fill_null(False).alias("activa"),
    ).select(
        "activo_id", "especie", "nemotecnico", "nombre", "emisor_id", "clase_accion", "clase_activo_id", "pais_id",
        "moneda_id", "mic", "sector_sfc", "iba_categoria", "primera_negociacion", "ultima_negociacion",
        "dias_con_negociacion", "activa", "en_indice_vigente", "visto_desde",
    ).sort("activo_id")


def construir(desde: date = date(1991, 1, 1), hasta: date | None = None) -> dict[str, str]:
    hasta = hasta or date(date.today().year + 1, 12, 31)
    d = dimensiones()
    mics = [p["mic"] for p in d["paises"] if p.get("activo")]
    tablas = {
        "dim_fecha": dim_fecha(desde, hasta),
        "dim_sesion": dim_sesion(mics, max(desde, date(2000, 1, 1)), hasta),
        "dim_pais": pl.DataFrame(d["paises"]),
        "dim_moneda": pl.DataFrame(d["monedas"]),
        "dim_clase_activo": pl.DataFrame(d["clases_activo"]),
        "dim_indice": pl.DataFrame(d["indices"]),
        "dim_fuente": dim_fuente(),
    }
    activos = dim_activo()
    if activos is not None:
        tablas["dim_activo"] = activos
    rutas = {nombre: str(storage.escribir_completa("gold", nombre, df)) for nombre, df in tablas.items()}
    if eventos.construir_gold() is not None:
        rutas["fact_evento_corporativo"] = str(storage.data_dir() / "gold" / "fact_evento_corporativo")
    if dividendos.construir_gold() is not None:
        rutas["fact_dividendo"] = str(storage.data_dir() / "gold" / "fact_dividendo")
    tabla_splits, resumen_splits = splits.construir()
    if resumen_splits:
        rutas["fact_split"] = "revision/splits.csv (" + ", ".join(f"{k} {v}" for k, v in resumen_splits.items()) + ")"
    _, resumen_web = dividendos_web.construir()
    if resumen_web.get("pagos"):
        rutas["fact_dividendo_web"] = "revision/dividendos_web.csv (" + ", ".join(
            f"{k} {v}" for k, v in resumen_web.items()) + ")"
    _, resumen_dist = distribuciones.construir()
    if resumen_dist.get("filas"):
        rutas["fact_distribucion_especie"] = "config/eventos_capital.yaml (" + ", ".join(
            f"{k} {v}" for k, v in resumen_dist.items()) + ")"
    tabla_retornos, resumen = retornos.construir()
    if tabla_retornos is not None:
        detalle = ", ".join(f"{k} {v}" for k, v in resumen.items())
        rutas["fact_retorno_diario"] = f"{storage.data_dir() / 'gold' / 'fact_retorno_diario'} ({detalle})"
    tabla_liquidez, resumen_liq = liquidez.construir()
    if tabla_liquidez is not None:
        rutas["fact_liquidez_diaria"] = "gold/fact_liquidez_diaria (" + ", ".join(
            f"{k} {v}" for k, v in resumen_liq.items()) + ")"
    tabla_fund, resumen_fund = fundamentales.construir()
    if tabla_fund is not None:
        rutas["fact_fundamental"] = "gold/fact_fundamental (" + ", ".join(
            f"{k} {v}" for k, v in resumen_fund.items()) + ")"
    resumen_mercado = mercado.construir()
    if resumen_mercado:
        rutas["dim_emisor y fact_precio_diario"] = ", ".join(f"{k} {v}" for k, v in resumen_mercado.items())
    resumen_macro = macro.construir()
    if resumen_macro:
        rutas["dim_indicador_macro y fact_macro"] = ", ".join(f"{k} {v}" for k, v in resumen_macro.items())
    return rutas

