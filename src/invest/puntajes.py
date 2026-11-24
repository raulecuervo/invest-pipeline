"""Puntajes de 1 a 10 por factor, matriz fundamental × técnico y screeners (informativos).

Implementa la escala del diseño (config/escala.yaml): cada métrica se convierte en un z robusto
frente a su grupo (mediana y desviación absoluta mediana del universo líquido del mes, o del sector
si tiene suficientes pares), limitado a ±3, con su dirección; el z de un factor es el promedio de
los z disponibles de sus métricas; el global y los ejes de la matriz combinan los z de los factores
y recién entonces se pasan a 1-10 (5,5 + 1,5 z). Dato faltante = excluido, nunca 5.

Fechas: cada fin de mes y la última sesión con precio (las de ``gold/fact_valoracion``). Las demás
fuentes entran con lo último conocido hasta 10 días antes (as-of): momentum del mes
(``fact_factor_mensual``), técnico del día, riesgo de 1 año en pesos, métricas fundamentales por
``fecha_disponible`` y el puntaje de los factores validados (``fact_puntaje_mensual``).

Salidas:
- ``gold/fact_puntaje`` (largo): por fecha, especie y elemento (cada métrica y cada factor, los ejes
  ``fundamental`` y ``tecnico`` y el ``global``): valor, z, puntaje, banda, percentil dentro del grupo,
  grupo de comparación y número de pares. Es la auditoría de cada número.
- ``gold/fact_matriz`` (ancho): una fila por fecha y especie con las métricas, los puntajes, el
  cuadrante (``ambos_altos``, ``fundamental_alto``, ``tecnico_alto``, ``ambos_bajos``) y ``decision``,
  que queda vacía mientras los factores de la matriz no estén validados (``estado`` = informativo).
  El eje técnico queda vacío si la acción no es líquida ese día (el diseño lo pide como N/A).
- ``gold/fact_screener_corrida``: las acciones que cumplen cada screener de config/screeners.yaml en
  cada fecha, con ``nueva`` = no estaba en la corrida anterior.
"""

from __future__ import annotations

import polars as pl

from invest import storage
from invest.config import cargar_yaml

TOLERANCIA = "10d"


def config() -> dict:
    return cargar_yaml("escala.yaml")


def _asof(base: pl.DataFrame, otra: pl.DataFrame, columnas: list[str], fecha_otra: str = "fecha",
          por: str = "especie", tolerancia: str | None = TOLERANCIA) -> pl.DataFrame:
    o = otra.select(por, pl.col(fecha_otra).alias("_f"), *columnas).sort("_f")
    return base.sort("fecha").join_asof(o, left_on="fecha", right_on="_f", by=por, strategy="backward",
                                        check_sortedness=False, tolerance=tolerancia).drop("_f")


def panel() -> pl.DataFrame | None:
    """Una fila por fecha y especie con todas las métricas que usan los puntajes y los screeners."""
    valoracion = storage.leer_tabla("gold", "fact_valoracion")
    activos = storage.leer_dim("dim_activo")
    if valoracion is None or valoracion.is_empty() or activos is None:
        return None
    base = valoracion.join(activos.select("especie", "nombre", "sector_sfc", "clase_accion"), on="especie", how="left")
    factores = storage.leer_tabla("gold", "fact_factor_mensual")
    if factores is not None:
        mom = factores.filter(pl.col("factor") == "momentum_12_1").select(
            "fecha", "especie", pl.col("valor").alias("momentum_12_1"), pl.col("percentil").alias("momentum_12_1_percentil"))
        base = _asof(base, mom, ["momentum_12_1", "momentum_12_1_percentil"])
    tecnico = storage.leer_tabla("gold", "fact_tecnico_diario")
    if tecnico is not None:
        base = _asof(base, tecnico, ["distancia_sma200", "tendencia_alcista", "caida_desde_max_52s", "rsi_14",
                                     "tecnico_valido"])
    riesgo = storage.leer_tabla("gold", "fact_riesgo")
    if riesgo is not None:
        r = riesgo.filter((pl.col("ventana") == "1a") & (pl.col("moneda") == "COP"))
        base = _asof(base, r, ["volatilidad_anual", "drawdown_maximo", "beta"])
    metricas = storage.leer_tabla("gold", "fact_metrica_fundamental")
    if metricas is not None and not metricas.is_empty():
        # Orden de preferencia de bases: el consolidado (XBRL) primero; los separados anuales de
        # Supersociedades son el respaldo de los emisores sin XBRL
        for metrica, orden in (("apalancamiento", ["separado_anual", "consolidado"]),
                               ("crecimiento_upa_12m", ["separado"]),
                               ("crecimiento_ingresos_12m", ["separado_anual", "consolidado"])):
            m = (metricas.filter((pl.col("metrica") == metrica) & pl.col("base").is_in(orden))
                 .with_columns(pl.col("base").replace_strict({b: i for i, b in enumerate(orden)},
                                                             return_dtype=pl.Int32).alias("_pref"))
                 .sort(["emisor_id", "fecha_disponible", "_pref"])
                 .unique(subset=["emisor_id", "fecha_disponible"], keep="last", maintain_order=True)
                 .select("emisor_id", "fecha_disponible", pl.col("valor").alias(metrica)))
            base = _asof(base, m, [metrica], fecha_otra="fecha_disponible", por="emisor_id", tolerancia="460d")
    validado = storage.leer_tabla("gold", "fact_puntaje_mensual")
    if validado is not None and not validado.is_empty():
        v = validado.with_columns(((pl.col("acciones_mes") - pl.col("posicion") + 0.5) / pl.col("acciones_mes"))
                                  .alias("percentil_validado")).select("fecha", "especie", "percentil_validado",
                                                                      pl.col("tercio").alias("tercio_validado"))
        base = _asof(base, v, ["percentil_validado", "tercio_validado"])
    for c, t in (("momentum_12_1", pl.Float64), ("momentum_12_1_percentil", pl.Float64), ("tecnico_valido", pl.Boolean),
                 ("distancia_sma200", pl.Float64), ("tendencia_alcista", pl.Boolean), ("caida_desde_max_52s", pl.Float64),
                 ("rsi_14", pl.Float64), ("volatilidad_anual", pl.Float64), ("drawdown_maximo", pl.Float64),
                 ("beta", pl.Float64), ("apalancamiento", pl.Float64), ("crecimiento_upa_12m", pl.Float64),
                 ("crecimiento_ingresos_12m", pl.Float64), ("percentil_validado", pl.Float64),
                 ("tercio_validado", pl.Utf8)):
        if c not in base.columns:
            base = base.with_columns(pl.lit(None, t).alias(c))
    # Universo líquido del mes: las acciones que tienen momentum ese mes (mismas reglas de factores.py)
    return base.with_columns(pl.col("momentum_12_1").is_not_null().alias("en_universo")).sort(["fecha", "especie"])


def _puntaje(z: pl.Expr, cfg: dict) -> pl.Expr:
    e = cfg["escala"]
    return (pl.lit(e["centro"]) + e["pendiente"] * z).clip(1.0, 10.0).round(1)


def _banda(p: pl.Expr, cfg: dict) -> pl.Expr:
    bandas = sorted(cfg["escala"]["bandas"], key=lambda b: -b["desde"])
    expr = pl.when(p.is_null()).then(None)
    for b in bandas:
        expr = expr.when(p >= b["desde"]).then(pl.lit(b["nombre"]))
    return expr.otherwise(pl.lit(bandas[-1]["nombre"]))


def calcular(p: pl.DataFrame, cfg: dict | None = None, validados: set[str] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    cfg = cfg or config()
    e = cfg["escala"]
    zmax, minimo = float(e["z_max"]), int(e["minimo_pares"])
    # Grupo de comparación: sector con suficientes pares en el universo del mes; si no, el universo
    pares = p.filter(pl.col("en_universo")).group_by("fecha", "sector_sfc").agg(pl.len().alias("_n_sector"))
    p = p.join(pares, on=["fecha", "sector_sfc"], how="left").with_columns(
        pl.when(pl.col("_n_sector").fill_null(0) >= minimo).then(pl.col("sector_sfc")).otherwise(pl.lit("universo"))
        .alias("grupo_comparacion"))
    p = p.with_columns(pl.when(pl.col("en_universo")).then(pl.col("grupo_comparacion")).alias("_grupo_ref"))
    largo, z_factor = [], {}
    for factor, fdef in cfg["factores"].items():
        zs = []
        for metrica, mdef in fdef["metricas"].items():
            d = float(mdef.get("direccion", 1))
            x = pl.col(metrica).cast(pl.Float64)
            ref = (p.filter(pl.col("en_universo") & x.is_not_null() & x.is_finite())
                   .group_by("fecha", "grupo_comparacion")
                   .agg(x.median().alias("_med"), (x - x.median()).abs().median().alias("_mad"), pl.len().alias("_pares")))
            q = p.join(ref, on=["fecha", "grupo_comparacion"], how="left").with_columns(
                pl.when(x.is_finite() & (pl.col("_mad") > 0))
                .then((d * (x - pl.col("_med")) / (1.4826 * pl.col("_mad"))).clip(-zmax, zmax))
                .otherwise(None).alias("_z"))
            q = q.with_columns(
                pl.when(pl.col("_z").is_not_null() & pl.col("en_universo"))
                .then((pl.col("_z").rank("average").over("fecha", "grupo_comparacion") - 0.5)
                      / pl.col("_z").is_not_null().sum().over("fecha", "grupo_comparacion"))
                .alias("_pct"))
            largo.append(q.select("fecha", "especie", pl.lit(metrica).alias("elemento"), pl.lit("metrica").alias("nivel"),
                                  pl.lit(factor).alias("factor"), x.cast(pl.Float64).alias("valor"),
                                  pl.col("_z").alias("z"), pl.col("_pct").alias("percentil"), "grupo_comparacion",
                                  pl.col("_pares").alias("pares")))
            zs.append(q.select("fecha", "especie", pl.col("_z").alias(metrica)))
        t = zs[0]
        for extra in zs[1:]:
            t = t.join(extra, on=["fecha", "especie"])
        nombres = list(fdef["metricas"])
        t = t.with_columns(pl.mean_horizontal(*nombres).alias("_zf"),
                           pl.sum_horizontal(*[pl.col(n).is_not_null().cast(pl.Int32) for n in nombres]).alias("_m"))
        z_factor[factor] = t.select("fecha", "especie", pl.col("_zf").alias(factor), pl.col("_m").alias(f"_m_{factor}"))
    m = p
    for factor, t in z_factor.items():
        m = m.join(t, on=["fecha", "especie"], how="left")

    def combinar(nombres: list[str], pesos: dict[str, float]) -> pl.Expr:
        num = pl.sum_horizontal(*[pl.col(n).fill_null(0) * pesos[n] for n in nombres])
        den = pl.sum_horizontal(*[pl.col(n).is_not_null().cast(pl.Float64) * pesos[n] for n in nombres])
        return pl.when(den > 0).then((num / den).clip(-zmax, zmax)).otherwise(None)

    pesos = {f: float(d.get("peso", 1)) for f, d in cfg["factores"].items()}
    ejes = cfg["matriz"]
    m = m.with_columns(
        combinar(list(cfg["factores"]), pesos).alias("global"),
        combinar(ejes["fundamental"], pesos).alias("fundamental"),
        pl.when(pl.col("tecnico_valido").fill_null(False)).then(combinar(ejes["tecnico"], pesos)).otherwise(None)
        .alias("tecnico"),
    )
    agregados = list(cfg["factores"]) + ["fundamental", "tecnico", "global"]
    for a in agregados:
        largo.append(m.select("fecha", "especie", pl.lit(a).alias("elemento"),
                              pl.lit("factor" if a in cfg["factores"] else ("global" if a == "global" else "eje")).alias("nivel"),
                              pl.lit(a if a in cfg["factores"] else None, pl.Utf8).alias("factor"),
                              pl.lit(None, pl.Float64).alias("valor"), pl.col(a).alias("z"),
                              pl.lit(None, pl.Float64).alias("percentil"), "grupo_comparacion",
                              pl.lit(None, pl.UInt32).alias("pares")))
    puntaje = (pl.concat(largo, how="vertical_relaxed").filter(pl.col("z").is_not_null() | (pl.col("nivel") == "metrica"))
               .with_columns(_puntaje(pl.col("z"), cfg).alias("puntaje"))
               .with_columns(_banda(pl.col("puntaje"), cfg).alias("banda"))
               .sort(["fecha", "especie", "nivel", "elemento"]))
    # Matriz y decisión
    umbral = float(ejes["umbral"])
    m = m.with_columns(*[_puntaje(pl.col(a), cfg).alias(f"puntaje_{a}") for a in agregados])
    alto_f, alto_t = pl.col("puntaje_fundamental") >= umbral, pl.col("puntaje_tecnico") >= umbral
    m = m.with_columns(
        pl.when(pl.col("puntaje_fundamental").is_null() | pl.col("puntaje_tecnico").is_null()).then(None)
        .when(alto_f & alto_t).then(pl.lit("ambos_altos")).when(alto_f).then(pl.lit("fundamental_alto"))
        .when(alto_t).then(pl.lit("tecnico_alto")).otherwise(pl.lit("ambos_bajos")).alias("cuadrante"))
    validados = validados if validados is not None else _factores_validados()
    metricas_matriz = {mt for f in ejes["fundamental"] + ejes["tecnico"] for mt in cfg["factores"][f]["metricas"]}
    matriz_validada = metricas_matriz <= validados
    m = m.with_columns(
        pl.lit("validado" if matriz_validada else "informativo").alias("estado"),
        (pl.col("cuadrante").replace_strict(cfg["decisiones"], default=None) if matriz_validada
         else pl.lit(None, pl.Utf8)).alias("decision"))
    columnas_matriz = [
        "fecha", "especie", "emisor_id", "nombre", "sector_sfc", "clase_accion", "en_universo", "grupo_comparacion",
        "precio", "capitalizacion", "capitalizacion_usd", "p_vl", "p_u", "rendimiento_utilidad",
        "rendimiento_dividendo_12m", "roe_12m", "apalancamiento", "crecimiento_upa_12m", "crecimiento_ingresos_12m",
        "momentum_12_1", "momentum_12_1_percentil", "distancia_sma200", "tendencia_alcista", "caida_desde_max_52s",
        "rsi_14", "tecnico_valido", "volatilidad_anual", "drawdown_maximo", "beta",
        *[f"puntaje_{a}" for a in agregados], *[f"_m_{f}" for f in cfg["factores"]],
        "cuadrante", "estado", "decision", "percentil_validado", "tercio_validado",
    ]
    matriz = m.select(columnas_matriz).rename({f"_m_{f}": f"metricas_{f}" for f in cfg["factores"]}) \
        .sort(["fecha", "especie"])
    return puntaje, matriz


def _factores_validados() -> set[str]:
    v = storage.leer_tabla("gold", "fact_validacion_factor")
    if v is None:
        return set()
    return set(v.filter(pl.col("veredicto") == "validado").get_column("factor").to_list())


def screeners(matriz: pl.DataFrame) -> pl.DataFrame:
    definiciones = cargar_yaml("screeners.yaml")["screeners"]
    partes = []
    for sid, d in definiciones.items():
        cumple = matriz.filter(pl.sql_expr(d["condicion"]).fill_null(False))
        partes.append(cumple.select("fecha", pl.lit(sid).alias("screener_id"), "especie"))
    if not partes:
        return pl.DataFrame(schema={"fecha": pl.Date, "screener_id": pl.Utf8, "especie": pl.Utf8, "nueva": pl.Boolean})
    corridas = pl.concat(partes)
    fechas = matriz.select("fecha").unique().sort("fecha").with_columns(pl.col("fecha").shift(1).alias("_anterior"))
    marca = corridas.join(fechas, left_on="fecha", right_on="_anterior", how="inner", suffix="_sig") \
        .select("screener_id", "especie", pl.col("fecha_sig").alias("fecha"), pl.lit(True).alias("_estaba"))
    return (corridas.join(marca, on=["screener_id", "especie", "fecha"], how="left")
            .with_columns(pl.col("_estaba").is_null().alias("nueva")).drop("_estaba")
            .sort(["fecha", "screener_id", "especie"]))


def construir() -> dict:
    p = panel()
    if p is None or p.is_empty():
        return {}
    puntaje, matriz = calcular(p)
    corridas = screeners(matriz)
    storage.escribir_por_anio("gold", "fact_puntaje", puntaje, "fecha")
    storage.escribir_por_anio("gold", "fact_matriz", matriz, "fecha")
    storage.escribir_por_anio("gold", "fact_screener_corrida", corridas, "fecha")
    ultimo = matriz.get_column("fecha").max()
    hoy = matriz.filter(pl.col("fecha") == ultimo)
    return {"fecha": ultimo, "acciones": hoy.height, "en_universo": hoy.filter(pl.col("en_universo")).height,
            "con_global": hoy.filter(pl.col("puntaje_global").is_not_null()).height,
            "estado": hoy.get_column("estado")[0] if hoy.height else None,
            "screeners_hoy": corridas.filter(pl.col("fecha") == ultimo).height}
