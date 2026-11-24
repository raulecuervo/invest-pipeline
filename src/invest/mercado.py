"""Emisores, precio diario unificado y capitalización bursátil.

- ``gold/dims/dim_emisor``: una fila por emisor de acciones (prefijo de la especie de la SFC), con
  NIT, nombre, nombre corto, sector, moneda de sus estados financieros y sus códigos del SIMEV.
- ``gold/fact_precio_diario``: por especie y día con negocios (los de ``fact_retorno_diario``) o con
  lectura de la Bolsa de Colombia. Junta el precio promedio ponderado de la SFC con el cierre,
  apertura, máximo y mínimo de la Bolsa cuando hay lectura manual, el retorno total y su índice,
  y la capitalización: precio × acciones en circulación de la clase de la especie conocidas a esa
  fecha (anexo 211 del SIMEV, por ``fecha_disponible``), en pesos y en dólares (TRM con
  ``fecha_mercado`` = día del precio, como dice config/convenciones.yaml).

El precio ajustado no se guarda porque cambia hacia atrás con cada dividendo nuevo: es
``indice_rt × precio del último día / indice_rt del último día`` (vista ``v_precio_ajustado`` del
modelo DuckDB).
"""

from __future__ import annotations

import polars as pl

from invest import storage
from invest.config import cargar_yaml
from invest.ingest.sfc_niif import emisores

# Clase de la especie (dim_activo.clase_accion) → concepto de acciones en circulación (gold/fact_fundamental)
ACCIONES_POR_CLASE = {
    "ordinaria": "acciones_ordinarias",
    "preferencial": "acciones_preferenciales",
    "privilegiada": "acciones_privilegiadas",
}
MINIMO_ACCIONES = 100_000  # menos que esto en el 211 es un campo mal diligenciado (p. ej. 1.338)


def dim_emisor(activos: pl.DataFrame | None = None) -> pl.DataFrame | None:
    from invest.tablero import nombre_corto

    activos = activos if activos is not None else storage.leer_dim("dim_activo")
    if activos is None or activos.is_empty():
        return None
    cortos = cargar_yaml("tablero.yaml").get("nombres_cortos") or {}
    rnve = {e["emisor_id"]: e for e in emisores()}
    filas = []
    for (emisor_id,), g in activos.sort(["activa", "dias_con_negociacion"], descending=True, nulls_last=True) \
            .group_by(["emisor_id"], maintain_order=True):
        principal = g.row(0, named=True)  # la especie activa con más días de negocio
        e = rnve.get(emisor_id, {})
        sectores = g.get_column("sector_sfc").drop_nulls()
        filas.append({
            "emisor_id": emisor_id,
            "nombre": e.get("nombre") or nombre_corto(principal["especie"], principal["nombre"], "ordinaria", {}),
            "nombre_corto": nombre_corto(principal["especie"], principal["nombre"], "ordinaria", cortos),
            "nit": e.get("nit"),
            "sector_sfc": sectores.mode().sort()[0] if not sectores.is_empty() else None,
            "pais_id": "CO",
            "moneda_estados": e.get("moneda_informes", "COP"),
            "estandar_contable": "NIIF",
            "en_rnve": bool(e),
            "codigos_simev": ", ".join(f"{c['tipo']}-{c['codigo']}" for c in e.get("codigos", [])) or None,
            "especies": g.height,
            "especies_activas": int(g.get_column("activa").fill_null(False).sum()),
            "especie_principal": principal["especie"],
            "activo": bool(g.get_column("activa").fill_null(False).any()),
        })
    return pl.DataFrame(filas).sort("emisor_id")


def _acciones_en_circulacion() -> pl.DataFrame:
    """Acciones por emisor, concepto (clase) y fecha desde la que se conocen."""
    vacio = pl.DataFrame(schema={"emisor_id": pl.Utf8, "concepto": pl.Utf8, "fecha_disponible": pl.Date,
                                 "acciones": pl.Float64})
    fund = storage.leer_tabla("gold", "fact_fundamental")
    if fund is None or fund.is_empty():
        return vacio
    acc = (fund.filter(pl.col("concepto").str.starts_with("acciones_") & (pl.col("valor") >= MINIMO_ACCIONES))
           .select("emisor_id", "concepto", "fecha_disponible", pl.col("valor").alias("acciones"))
           .sort(["emisor_id", "concepto", "fecha_disponible"])
           .unique(subset=["emisor_id", "concepto", "fecha_disponible"], keep="last", maintain_order=True))
    # Emisores con una sola clase y sin desglose: el total vale para la ordinaria
    con_clase = acc.filter(pl.col("concepto") != "acciones_circulacion").select("emisor_id", "fecha_disponible")
    solo_total = (acc.filter(pl.col("concepto") == "acciones_circulacion")
                  .join(con_clase, on=["emisor_id", "fecha_disponible"], how="anti")
                  .with_columns(pl.lit("acciones_ordinarias").alias("concepto")))
    return pl.concat([acc.filter(pl.col("concepto") != "acciones_circulacion"), solo_total]).select(vacio.columns)


def _trm() -> pl.DataFrame:
    fx = storage.leer_tabla("silver", "fact_fx_diaria")
    if fx is None or fx.is_empty():
        return pl.DataFrame(schema={"fecha_mercado": pl.Date, "trm": pl.Float64})
    return (fx.filter((pl.col("moneda_origen") == "USD") & (pl.col("moneda_destino") == "COP"))
            .select("fecha_mercado", pl.col("tasa").alias("trm")).drop_nulls()
            .unique(subset=["fecha_mercado"], keep="last").sort("fecha_mercado"))


def fact_precio_diario() -> pl.DataFrame | None:
    retornos = storage.leer_tabla("gold", "fact_retorno_diario")
    activos = storage.leer_dim("dim_activo")
    if retornos is None or retornos.is_empty() or activos is None:
        return None
    base = retornos.select(
        "fecha", "especie", "precio_promedio", "valor_transado", "dias_desde_anterior", "dividendo",
        "distribucion", "split_factor", "retorno_precio", "retorno_total", "indice_rt", "salto_atipico")
    bvc = storage.leer_tabla("silver", "fact_precio_bvc")
    if bvc is not None and not bvc.is_empty():
        bvc = (bvc.filter(pl.col("especie").is_not_null())
               .select("fecha", "especie", "nemotecnico", pl.col("cierre").alias("cierre_bvc"),
                       pl.col("apertura").alias("apertura_bvc"), pl.col("maximo").alias("maximo_bvc"),
                       pl.col("minimo").alias("minimo_bvc"), pl.col("volumen_cop").alias("volumen_bvc"),
                       pl.col("cantidad").alias("cantidad_bvc"))
               .unique(subset=["fecha", "especie"], keep="last"))
        base = base.join(bvc, on=["fecha", "especie"], how="full", coalesce=True)
    else:
        base = base.with_columns(*[pl.lit(None, t).alias(c) for c, t in (
            ("nemotecnico", pl.Utf8), ("cierre_bvc", pl.Float64), ("apertura_bvc", pl.Float64),
            ("maximo_bvc", pl.Float64), ("minimo_bvc", pl.Float64), ("volumen_bvc", pl.Float64),
            ("cantidad_bvc", pl.Float64))])
    base = base.join(activos.select("especie", "activo_id", "emisor_id", "clase_accion"), on="especie", how="left")
    base = base.with_columns(
        pl.coalesce("cierre_bvc", "precio_promedio").alias("precio"),
        pl.when(pl.col("cierre_bvc").is_not_null()).then(pl.lit("bvc_cierre")).otherwise(pl.lit("sfc_promedio"))
        .alias("fuente_precio"),
        pl.col("clase_accion").replace_strict(ACCIONES_POR_CLASE, default=None).alias("_concepto"),
    )
    # Acciones de la clase conocidas a cada fecha (as-of por fecha_disponible)
    acc = _acciones_en_circulacion().rename({"concepto": "_concepto"})
    base = base.sort("fecha").join_asof(
        acc.sort("fecha_disponible"), left_on="fecha", right_on="fecha_disponible",
        by=["emisor_id", "_concepto"], strategy="backward", check_sortedness=False,
    ).rename({"fecha_disponible": "fecha_acciones", "acciones": "acciones_circulacion"})
    base = base.sort("fecha").join_asof(_trm(), left_on="fecha", right_on="fecha_mercado", strategy="backward",
                                        check_sortedness=False, tolerance="7d")
    df = base.with_columns(
        (pl.col("precio") * pl.col("acciones_circulacion")).alias("capitalizacion"),
        (pl.col("precio") / pl.col("trm")).alias("precio_usd"),
    ).with_columns((pl.col("capitalizacion") / pl.col("trm")).alias("capitalizacion_usd"))
    return df.select(
        "fecha", "especie", "activo_id", "emisor_id", "nemotecnico", "precio", "fuente_precio",
        "precio_promedio", "valor_transado", "cierre_bvc", "apertura_bvc", "maximo_bvc", "minimo_bvc",
        "volumen_bvc", "cantidad_bvc", "dias_desde_anterior", "dividendo", "distribucion", "split_factor",
        "retorno_precio", "retorno_total", "indice_rt", "salto_atipico", "acciones_circulacion",
        "fecha_acciones", "capitalizacion", "trm", "precio_usd", "capitalizacion_usd",
    ).sort(["especie", "fecha"])


def construir() -> dict:
    resumen: dict = {}
    emisores_df = dim_emisor()
    if emisores_df is not None:
        storage.escribir_completa("gold", "dim_emisor", emisores_df)
        resumen["emisores"] = emisores_df.height
    precios = fact_precio_diario()
    if precios is not None:
        storage.escribir_por_anio("gold", "fact_precio_diario", precios, "fecha")
        ultimo = precios.get_column("fecha").max()
        hoy = precios.filter(pl.col("fecha") == ultimo)
        resumen |= {"filas_precio": precios.height, "ultimo_dia": ultimo,
                    "con_capitalizacion_ultimo_dia": hoy.filter(pl.col("capitalizacion").is_not_null()).height,
                    "con_cierre_bvc": precios.filter(pl.col("cierre_bvc").is_not_null()).height}
    return resumen
