"""Puntajes de 1 a 10 por factor, matriz fundamental × técnico y screeners (informativos).

Implementa la escala del diseño (config/escala.yaml): cada métrica se convierte en un z robusto
frente a su grupo (mediana y desviación absoluta mediana del universo líquido del mes, o del sector
si tiene suficientes pares), limitado a ±3, con su dirección; el z de un factor es el promedio de
los z disponibles de sus métricas; el global y los ejes de la matriz combinan los z de los factores
y recién entonces se pasan a 1-10 (5,5 + 1,5 z). Dato faltante = excluido, nunca 5.

Bancos (sección ``bancos`` de config/escala.yaml): para los emisores que son bancos (sector de la SFC o
con cifras del CUIF en ``gold/fact_metrica_banco``, lo que incluye a los holdings Grupo Cibest,
Davivienda Group y Grupo Aval) los factores listados ahí usan las métricas de banco (ROTE, costo del
riesgo, eficiencia) en lugar de las genéricas (ROE de los anexos, apalancamiento). Esas métricas se
comparan solo entre bancos: la referencia son las entidades individuales del CUIF con lo último
publicado a la fecha (``referencia_bancos``), con su propio mínimo de pares, y el grupo de comparación
de la auditoría es ``bancos``.

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
# Columnas de las bandas de valoración (gold/fact_banda_valoracion_mensual) que pasan a fact_matriz
COLUMNAS_BANDAS = (("z_p_vl_5a", pl.Float64), ("quiebre_p_vl_5a", pl.Date), ("z_p_u_5a", pl.Float64),
                   ("quiebre_p_u_5a", pl.Date))


def config() -> dict:
    return cargar_yaml("escala.yaml")


def _asof(base: pl.DataFrame, otra: pl.DataFrame, columnas: list[str], fecha_otra: str = "fecha",
          por: str = "especie", tolerancia: str | None = TOLERANCIA) -> pl.DataFrame:
    o = otra.select(por, pl.col(fecha_otra).alias("_f"), *columnas).sort("_f")
    return base.sort("fecha").join_asof(o, left_on="fecha", right_on="_f", by=por, strategy="backward",
                                        check_sortedness=False, tolerance=tolerancia).drop("_f")


def metricas_banco(cfg: dict) -> list[str]:
    """Métricas de gold/fact_metrica_banco que usa la sección ``bancos`` (con sus respaldos), en orden."""
    salida: list[str] = []
    for fdef in ((cfg.get("bancos") or {}).get("factores") or {}).values():
        for m, d in (fdef.get("metricas") or {}).items():
            for x in (m, (d or {}).get("respaldo")):
                if x and x not in salida:
                    salida.append(x)
    return salida


def _leer_metrica_banco() -> pl.DataFrame | None:
    return storage.leer_tabla("gold", "fact_metrica_banco")


def _tolerancia_bancos(cfg: dict) -> str:
    return f"{int((cfg.get('bancos') or {}).get('tolerancia_dias', 100))}d"


def referencia_bancos(fechas: list, cfg: dict | None = None, mb: pl.DataFrame | None = None) -> pl.DataFrame | None:
    """Mediana, desviación absoluta mediana y número de bancos por fecha y métrica de banco.

    Entran las entidades individuales del CUIF (``base = cuif_banco``, una vez por entidad aunque dos
    emisores compartan banco), con su último dato disponible a la fecha dentro de la tolerancia."""
    cfg = cfg or config()
    nombres = metricas_banco(cfg)
    mb = mb if mb is not None else _leer_metrica_banco()
    if mb is None or mb.is_empty() or not nombres or not fechas:
        return None
    ind = (mb.filter((pl.col("base") == "cuif_banco") & pl.col("metrica").is_in(nombres))
           .unique(subset=["entidades", "fecha_corte", "metrica"], keep="first")
           .select("entidades", "metrica", "fecha_disponible", "valor").sort("fecha_disponible"))
    if ind.is_empty():
        return None
    grilla = ind.select("entidades", "metrica").unique().join(pl.DataFrame({"fecha": fechas}), how="cross")
    r = grilla.sort("fecha").join_asof(ind, left_on="fecha", right_on="fecha_disponible", by=["entidades", "metrica"],
                                       strategy="backward", check_sortedness=False,
                                       tolerance=_tolerancia_bancos(cfg))
    x = pl.col("valor")
    return (r.filter(x.is_not_null() & x.is_finite()).group_by("fecha", "metrica")
            .agg(x.median().alias("_med"), (x - x.median()).abs().median().alias("_mad"), pl.len().alias("_pares")))


def panel(cfg: dict | None = None) -> pl.DataFrame | None:
    """Una fila por fecha y especie con todas las métricas que usan los puntajes y los screeners."""
    cfg = cfg or config()
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
    # Métricas de banco (CUIF), por emisor y fecha disponible
    nombres_banco = metricas_banco(cfg)
    mb = _leer_metrica_banco()
    if mb is not None and not mb.is_empty() and nombres_banco:
        sel = mb.filter(pl.col("metrica").is_in(nombres_banco))
        if not sel.is_empty():
            w = sel.pivot(on="metrica", index=["emisor_id", "fecha_corte", "fecha_disponible", "entidades", "es_holding"],
                          values="valor", aggregate_function="first")
            presentes = [n for n in nombres_banco if n in w.columns]
            w = w.rename({"entidades": "entidad_banco", "es_holding": "banco_holding"})
            base = _asof(base, w, ["entidad_banco", "banco_holding", *presentes], fecha_otra="fecha_disponible",
                         por="emisor_id", tolerancia=_tolerancia_bancos(cfg))
    # Bandas de valoración (src/invest/bandas.py): z de P/VL y P/U a 5 años y el último quiebre en la
    # ventana, en las mismas fechas que fact_valoracion (para el screener de valoración histórica)
    bandas = storage.leer_tabla("gold", "fact_banda_valoracion_mensual")
    if bandas is not None and not bandas.is_empty():
        for ind in ("p_vl", "p_u"):
            b = bandas.filter((pl.col("indicador") == ind) & (pl.col("ventana") == "5a")).select(
                "fecha", "especie", pl.col("z").alias(f"z_{ind}_5a"), pl.col("quiebre").alias(f"quiebre_{ind}_5a"))
            base = base.join(b, on=["fecha", "especie"], how="left")
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
                 ("tercio_validado", pl.Utf8), *COLUMNAS_BANDAS, *_columnas_banco(cfg)):
        if c not in base.columns:
            base = base.with_columns(pl.lit(None, t).alias(c))
    # Una acción que no es líquida ese día no tiene un técnico confiable (sus medias abarcan negocios
    # esporádicos de meses o años): su distancia a la media de 200 no entra al momentum ni al global
    base = base.with_columns(
        pl.when(pl.col("tecnico_valido").fill_null(False)).then(pl.col("distancia_sma200")).otherwise(None)
        .alias("distancia_sma200"),
        _es_banco(cfg).alias("es_banco"))
    # Universo líquido del mes: las acciones que tienen momentum ese mes (mismas reglas de factores.py)
    return base.with_columns(pl.col("momentum_12_1").is_not_null().alias("en_universo")).sort(["fecha", "especie"])


def _columnas_banco(cfg: dict) -> list[tuple[str, pl.DataType]]:
    return [("entidad_banco", pl.Utf8), ("banco_holding", pl.Boolean), *[(n, pl.Float64) for n in metricas_banco(cfg)]]


def _es_banco(cfg: dict) -> pl.Expr:
    b = cfg.get("bancos") or {}
    es = pl.col("sector_sfc").is_in(list(b.get("sectores") or [])).fill_null(False)
    if b.get("incluir_mapeados", True):
        es = es | pl.col("entidad_banco").is_not_null()
    return es


def _puntaje(z: pl.Expr, cfg: dict) -> pl.Expr:
    e = cfg["escala"]
    return (pl.lit(e["centro"]) + e["pendiente"] * z).clip(1.0, 10.0).round(1)


def _banda(p: pl.Expr, cfg: dict) -> pl.Expr:
    bandas = sorted(cfg["escala"]["bandas"], key=lambda b: -b["desde"])
    expr = pl.when(p.is_null()).then(None)
    for b in bandas:
        expr = expr.when(p >= b["desde"]).then(pl.lit(b["nombre"]))
    return expr.otherwise(pl.lit(bandas[-1]["nombre"]))


def _metricas_del_factor(factor: str, fdef: dict, cfg: dict) -> list[tuple[str, dict, pl.Expr, bool]]:
    """(métrica, definición, a qué filas aplica, si es de banco). Si la sección ``bancos`` redefine el
    factor, las métricas genéricas aplican solo a los que no son bancos y las de banco solo a bancos (el
    respaldo, solo donde falta la principal)."""
    es_b = pl.col("es_banco").fill_null(False)
    especificas = (((cfg.get("bancos") or {}).get("factores") or {}).get(factor) or {}).get("metricas") or {}
    lista = [(m, d, ~es_b if especificas else pl.lit(True), False) for m, d in fdef["metricas"].items()]
    for m, d in especificas.items():
        lista.append((m, d, es_b, True))
        if d.get("respaldo"):
            lista.append((d["respaldo"], d, es_b & pl.col(m).is_null(), True))
    vistos, salida = set(), []
    for item in lista:
        if item[0] not in vistos:
            vistos.add(item[0])
            salida.append(item)
    return salida


def calcular(p: pl.DataFrame, cfg: dict | None = None, validados: set[str] | None = None,
             ref_bancos: pl.DataFrame | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Puntajes y matriz. ``ref_bancos`` (de ``referencia_bancos``) es la referencia de las métricas de
    banco; sin ella se usan los bancos del propio panel (una vez por entidad del CUIF)."""
    cfg = cfg or config()
    e = cfg["escala"]
    zmax, minimo = float(e["z_max"]), int(e["minimo_pares"])
    minimo_bancos = int((cfg.get("bancos") or {}).get("minimo_pares", minimo))
    minimo_metrica = int(e.get("minimo_pares_metrica", 5))
    for c, t in (*_columnas_banco(cfg), *COLUMNAS_BANDAS):
        if c not in p.columns:
            p = p.with_columns(pl.lit(None, t).alias(c))
    if "es_banco" not in p.columns:
        p = p.with_columns(_es_banco(cfg).alias("es_banco"))
    p = p.with_columns(pl.coalesce(pl.col("entidad_banco"), pl.col("emisor_id")).alias("_entidad_ref"))
    # Grupo de comparación: sector con suficientes pares en el universo del mes; si no, el universo
    pares = p.filter(pl.col("en_universo")).group_by("fecha", "sector_sfc").agg(pl.len().alias("_n_sector"))
    p = p.join(pares, on=["fecha", "sector_sfc"], how="left").with_columns(
        pl.when(pl.col("_n_sector").fill_null(0) >= minimo).then(pl.col("sector_sfc")).otherwise(pl.lit("universo"))
        .alias("grupo_comparacion"))
    p = p.with_columns(pl.when(pl.col("en_universo")).then(pl.col("grupo_comparacion")).alias("_grupo_ref"))
    largo, z_factor = [], {}
    for factor, fdef in cfg["factores"].items():
        zs, nombres = [], []
        for metrica, mdef, aplica, de_banco in _metricas_del_factor(factor, fdef, cfg):
            d = float(mdef.get("direccion", 1))
            q = p.with_columns(aplica.alias("_aplica"))
            x = pl.when(pl.col("_aplica")).then(pl.col(metrica).cast(pl.Float64)).otherwise(None)
            if de_banco:
                # Solo entre bancos: referencia de las entidades del CUIF (o, sin ella, de los bancos del panel)
                if ref_bancos is not None:
                    ref = ref_bancos.filter(pl.col("metrica") == metrica).select("fecha", "_med", "_mad", "_pares")
                else:
                    # Todos los bancos con dato (también los que usan la principal cuando esta es respaldo)
                    xr = pl.col(metrica).cast(pl.Float64)
                    ref = (q.filter(pl.col("es_banco").fill_null(False) & xr.is_not_null() & xr.is_finite())
                           .unique(subset=["fecha", "_entidad_ref"]).group_by("fecha")
                           .agg(xr.median().alias("_med"), (xr - xr.median()).abs().median().alias("_mad"),
                                pl.len().alias("_pares")))
                ref = ref.filter(pl.col("_pares") >= minimo_bancos)
                q = q.join(ref, on="fecha", how="left")
                ventana, en_ref, grupo = ["fecha"], pl.col("_aplica"), pl.lit("bancos")
            else:
                con_dato = q.filter(pl.col("en_universo") & x.is_not_null() & x.is_finite())
                ref = (con_dato.group_by("fecha", "grupo_comparacion")
                       .agg(x.median().alias("_med"), (x - x.median()).abs().median().alias("_mad"),
                            pl.len().alias("_pares")))
                ref_u = (con_dato.group_by("fecha")
                         .agg(x.median().alias("_med_u"), (x - x.median()).abs().median().alias("_mad_u"),
                              pl.len().alias("_pares_u")))
                # Si en el sector hay menos de `minimo_pares_metrica` acciones con esta métrica (p. ej. el
                # crecimiento de los holdings cuando a varios se les anula por una escisión), la referencia
                # de esa métrica es el universo
                q = q.join(ref, on=["fecha", "grupo_comparacion"], how="left").join(ref_u, on="fecha", how="left")
                q = q.with_columns(((pl.col("grupo_comparacion") != "universo")
                                    & (pl.col("_pares").fill_null(0) < minimo_metrica)).alias("_al_universo"))
                q = q.with_columns(
                    pl.when(pl.col("_al_universo")).then(pl.col("_med_u")).otherwise(pl.col("_med")).alias("_med"),
                    pl.when(pl.col("_al_universo")).then(pl.col("_mad_u")).otherwise(pl.col("_mad")).alias("_mad"),
                    pl.when(pl.col("_al_universo")).then(pl.col("_pares_u")).otherwise(pl.col("_pares")).alias("_pares"),
                    pl.when(pl.col("_al_universo")).then(pl.lit("universo")).otherwise(pl.col("grupo_comparacion"))
                    .alias("_grupo_metrica"))
                ventana, en_ref, grupo = ["fecha", "_grupo_metrica"], pl.col("en_universo"), pl.col("_grupo_metrica")
            q = q.with_columns(
                pl.when(x.is_finite() & (pl.col("_mad") > 0))
                .then((d * (x - pl.col("_med")) / (1.4826 * pl.col("_mad"))).clip(-zmax, zmax))
                .otherwise(None).alias("_z"))
            q = q.with_columns(
                pl.when(pl.col("_z").is_not_null() & en_ref)
                .then((pl.col("_z").rank("average").over(*ventana) - 0.5) / pl.col("_z").is_not_null().sum().over(*ventana))
                .alias("_pct"))
            largo.append(q.filter(pl.col("_aplica")).select(
                "fecha", "especie", pl.lit(metrica).alias("elemento"), pl.lit("metrica").alias("nivel"),
                pl.lit(factor).alias("factor"), x.cast(pl.Float64).alias("valor"), pl.col("_z").alias("z"),
                pl.col("_pct").alias("percentil"), grupo.cast(pl.Utf8).alias("grupo_comparacion"),
                pl.col("_pares").alias("pares")))
            zs.append(q.select("fecha", "especie", pl.col("_z").alias(metrica)))
            nombres.append(metrica)
        t = zs[0]
        for extra in zs[1:]:
            t = t.join(extra, on=["fecha", "especie"])
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
    # El global necesita un mínimo de factores con dato: una acción con un solo factor (p. ej. solo el
    # rendimiento por dividendo de una acción que casi no negocia) no se compara con las que tienen cinco
    minimo_factores = int(e.get("minimo_factores_global", 1))
    con_dato = pl.sum_horizontal(*[pl.col(f).is_not_null().cast(pl.Int32) for f in cfg["factores"]])
    m = m.with_columns(
        pl.when(con_dato >= minimo_factores).then(combinar(list(cfg["factores"]), pesos)).otherwise(None).alias("global"),
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
        "es_banco", "banco_holding", *metricas_banco(cfg),
        *[f"puntaje_{a}" for a in agregados], *[f"_m_{f}" for f in cfg["factores"]],
        "cuadrante", "estado", "decision", "percentil_validado", "tercio_validado",
        *[c for c, _ in COLUMNAS_BANDAS],
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
    cfg = config()
    p = panel(cfg)
    if p is None or p.is_empty():
        return {}
    ref = referencia_bancos(p.get_column("fecha").unique().sort().to_list(), cfg)
    puntaje, matriz = calcular(p, cfg, ref_bancos=ref)
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
