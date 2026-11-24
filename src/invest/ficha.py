"""Matriz de factores: puntaje mensual de cada acción con los factores validados y su ficha.

Herramienta educativa. Ordena las acciones del universo líquido según los factores que pasaron la
validación estadística (``gold/fact_validacion_factor``, veredicto ``validado``); no emite señales de
compra o venta. El puntaje es el mismo que la validación mide como ``combinado_validados``, así que
la evidencia histórica que acompaña a la ficha corresponde exactamente a lo que se muestra.

1. **Puntaje** (``gold/fact_puntaje_mensual``): por mes y acción, el promedio de los percentiles de
   los factores validados, cada uno orientado según su hipótesis (1 = lo más favorable). Solo entran
   acciones con todos los factores validados. Posición y tercio dentro del mes (superior, medio,
   inferior) con la misma regla de la validación.
2. **Historia del tercio superior** (``gold/fact_puntaje_tercio``): por mes, el retorno siguiente
   promedio del tercio superior y del universo (pesos iguales), la rotación del tercio y el exceso
   neto de costos de compra y venta (``validacion.costo_ida`` de ``config/factores.yaml``).
3. **Ficha** (``gold/fact_ficha_accion``): al último fin de mes, una fila por acción activa, dentro o
   fuera del universo (con el motivo), con el puntaje, los percentiles y valores de todos los factores
   (los no validados quedan como contexto), su cambio frente a 1 y 3 meses, los meses seguidos en el
   tercio superior, liquidez, último negocio, valor patrimonial y utilidad por acción conocidos a esa
   fecha y las alertas de calidad de datos.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from invest import storage
from invest.factores import config as config_factores

TERCIOS = {2: "superior", 1: "medio", 0: "inferior"}


def factores_validados(validacion: pl.DataFrame | None) -> list[str]:
    if validacion is None or validacion.is_empty():
        return []
    return validacion.filter(pl.col("veredicto") == "validado").get_column("factor").to_list()


def puntajes(factores: pl.DataFrame, validados: list[str], cfg: dict | None = None) -> pl.DataFrame:
    """Puntaje combinado por mes y acción (ver docstring del módulo)."""
    cfg = cfg or config_factores()
    esquema = {"fecha": pl.Date, "especie": pl.Utf8, "puntaje": pl.Float64, "retorno_siguiente": pl.Float64,
               "acciones_mes": pl.UInt32, "posicion": pl.UInt32, "tercio": pl.Utf8}
    if not validados or factores is None or factores.is_empty():
        return pl.DataFrame(schema=esquema)
    signos = {k: int(cfg["factores"].get(k, {}).get("signo", 1)) for k in validados}
    q = int(cfg["validacion"]["cuantiles"])
    p = (factores.filter(pl.col("factor").is_in(validados))
         .with_columns(pl.when(pl.col("factor").replace_strict(signos, default=1) > 0)
                       .then(pl.col("percentil")).otherwise(1 - pl.col("percentil")).alias("_p"))
         .group_by("fecha", "especie")
         .agg(pl.col("_p").mean().alias("puntaje"), pl.col("retorno_siguiente").first(), pl.len().alias("_n"))
         .filter(pl.col("_n") == len(validados)).drop("_n")
         # Mismo orden que factores._metricas para que los empates caigan en el mismo tercio
         .sort(["fecha", "especie"])
         .with_columns(pl.len().over("fecha").cast(pl.UInt32).alias("acciones_mes"),
                       pl.col("puntaje").rank("ordinal").over("fecha").alias("_orden")))
    # Mismo agrupamiento que factores._metricas: (orden - 1) * cuantiles // n; el más alto = superior
    p = p.with_columns(
        ((pl.col("acciones_mes") - pl.col("_orden") + 1).cast(pl.UInt32)).alias("posicion"),
        (((pl.col("_orden") - 1) * q) // pl.col("acciones_mes")).alias("_g"),
    ).with_columns(
        pl.when(pl.col("_g") == q - 1).then(pl.lit("superior"))
        .when(pl.col("_g") == 0).then(pl.lit("inferior")).otherwise(pl.lit("medio")).alias("tercio")
    )
    return p.select(list(esquema)).sort(["fecha", "posicion"])


def historia_tercio(p: pl.DataFrame, inicio: date, costo_ida: float, minimo: int, cuantiles: int = 3) -> pl.DataFrame:
    """Retorno del tercio superior frente al universo, mes a mes, bruto y neto de costos."""
    filas, anterior = [], None
    for (fecha,), g in (p.filter((pl.col("fecha") >= inicio) & pl.col("retorno_siguiente").is_not_null())
                        .sort("fecha").group_by(["fecha"], maintain_order=True)):
        if g.height < minimo:
            continue
        # Tercio entre las acciones con retorno siguiente, igual que factores._metricas
        g = g.sort("especie").with_columns(pl.col("puntaje").rank("ordinal").alias("_o"))
        arriba = g.filter(((pl.col("_o") - 1) * cuantiles) // g.height == cuantiles - 1)
        conjunto = set(arriba.get_column("especie").to_list())
        rotacion = 1.0 if anterior is None else 1 - len(conjunto & anterior) / len(conjunto)
        r_sup = float(arriba.get_column("retorno_siguiente").mean())
        r_uni = float(g.get_column("retorno_siguiente").mean())
        filas.append({"fecha": fecha, "acciones": g.height, "acciones_superior": arriba.height,
                      "retorno_superior": r_sup, "retorno_universo": r_uni, "rotacion": rotacion,
                      "exceso": r_sup - r_uni, "exceso_neto": r_sup - r_uni - 2 * costo_ida * rotacion,
                      "retorno_superior_neto": r_sup - 2 * costo_ida * rotacion})
        anterior = conjunto
    if not filas:
        return pl.DataFrame()
    return pl.DataFrame(filas).with_columns(
        ((1 + pl.col("retorno_superior")).cum_prod() * 100).alias("indice_superior"),
        ((1 + pl.col("retorno_superior_neto")).cum_prod() * 100).alias("indice_superior_neto"),
        ((1 + pl.col("retorno_universo")).cum_prod() * 100).alias("indice_universo"),
    )


def _meses_seguidos_superior(p: pl.DataFrame, fecha: date) -> dict[str, int]:
    """Meses consecutivos en el tercio superior hasta ``fecha`` (incluida)."""
    salida: dict[str, int] = {}
    fechas = sorted(p.filter(pl.col("fecha") <= fecha).get_column("fecha").unique().to_list(), reverse=True)
    sup = {(r[0], r[1]) for r in p.filter(pl.col("tercio") == "superior").select("fecha", "especie").iter_rows()}
    for e in p.filter(pl.col("fecha") == fecha).get_column("especie").to_list():
        n = 0
        for f in fechas:
            if (f, e) not in sup:
                break
            n += 1
        salida[e] = n
    return salida


def _fundamentales_a(fecha: date, activos: pl.DataFrame) -> pl.DataFrame:
    """Valor patrimonial por acción (último conocido) y utilidad por acción del último cierre anual,
    por especie, con ``fecha_disponible`` ≤ ``fecha``."""
    vacio = pl.DataFrame(schema={"especie": pl.Utf8, "valor_patrimonial_accion": pl.Float64,
                                 "corte_valor_patrimonial": pl.Date, "utilidad_accion_anual": pl.Float64,
                                 "corte_utilidad_anual": pl.Date, "unidad_por_accion": pl.Utf8})
    fund = storage.leer_tabla("gold", "fact_fundamental")
    if fund is None or fund.is_empty():
        return vacio
    f = fund.filter((pl.col("fecha_disponible") <= fecha) & pl.col("unidad").str.ends_with("_por_accion"))

    def ultimo(sub: pl.DataFrame, valor: str, corte: str) -> pl.DataFrame:
        return (sub.sort(["emisor_id", "fecha_corte"]).group_by("emisor_id").agg(
            pl.col("valor").last().alias(valor), pl.col("fecha_corte").last().alias(corte),
            pl.col("unidad").last().alias(f"_u_{valor}")))

    vp = ultimo(f.filter(pl.col("concepto") == "valor_patrimonial_accion"), "valor_patrimonial_accion",
                "corte_valor_patrimonial")
    upa = ultimo(f.filter((pl.col("concepto") == "utilidad_accion") & (pl.col("fecha_corte").dt.month() == 12)),
                 "utilidad_accion_anual", "corte_utilidad_anual")
    por_emisor = vp.join(upa, on="emisor_id", how="full", coalesce=True).with_columns(
        pl.coalesce("_u_valor_patrimonial_accion", "_u_utilidad_accion_anual").alias("unidad_por_accion"))
    return (activos.select("especie", "emisor_id").join(por_emisor, on="emisor_id")
            .select(list(vacio.columns)))


def ficha(p: pl.DataFrame, factores: pl.DataFrame, validados: list[str], fecha: date | None = None,
          cfg: dict | None = None) -> pl.DataFrame:
    """Una fila por acción activa al fin de mes ``fecha`` (por omisión, el último)."""
    cfg = cfg or config_factores()
    u = cfg["universo"]
    fecha = fecha or factores.get_column("fecha").max()
    activos = storage.leer_dim("dim_activo")
    retornos = storage.leer_tabla("gold", "fact_retorno_diario")
    liquidez = storage.leer_tabla("gold", "fact_liquidez_diaria")
    if activos is None or retornos is None:
        return pl.DataFrame()

    # Factores del mes en ancho: valor y percentil de cada uno
    mes = factores.filter(pl.col("fecha") == fecha)
    ancho = None
    for nombre in cfg["factores"]:
        sub = mes.filter(pl.col("factor") == nombre).select(
            "especie", pl.col("valor").alias(f"{nombre}_valor"), pl.col("percentil").alias(f"{nombre}_percentil"))
        ancho = sub if ancho is None else ancho.join(sub, on="especie", how="full", coalesce=True)
    ancho = ancho if ancho is not None else pl.DataFrame(schema={"especie": pl.Utf8})

    # Puntaje del mes y su cambio
    fechas = sorted(p.get_column("fecha").unique().to_list())
    previas = [f for f in fechas if f < fecha]

    def percentil_en(f: date | None, alias: str) -> pl.DataFrame:
        if f is None:
            return pl.DataFrame(schema={"especie": pl.Utf8, alias: pl.Float64})
        return (p.filter(pl.col("fecha") == f)
                .with_columns(((pl.col("acciones_mes") - pl.col("posicion") + 0.5) / pl.col("acciones_mes")).alias(alias))
                .select("especie", alias))

    actual = (p.filter(pl.col("fecha") == fecha).select("especie", "puntaje", "posicion", "tercio", "acciones_mes")
              .join(percentil_en(fecha, "percentil_puntaje"), on="especie", how="left")
              .join(percentil_en(previas[-1] if previas else None, "percentil_puntaje_1m"), on="especie", how="left")
              .join(percentil_en(previas[-3] if len(previas) >= 3 else None, "percentil_puntaje_3m"),
                    on="especie", how="left"))
    seguidos = _meses_seguidos_superior(p, fecha)
    actual = actual.with_columns(
        pl.col("especie").replace_strict(seguidos, default=0, return_dtype=pl.Int64).alias("meses_seguidos_superior"))

    # Último negocio, precio y saltos atípicos del último año
    hasta = retornos.filter(pl.col("fecha") <= fecha)
    negocios = (hasta.sort("fecha").group_by("especie").agg(
        pl.col("fecha").last().alias("fecha_ultimo_negocio"),
        pl.col("precio_promedio").last().alias("precio"),
        pl.len().alias("dias_con_negocio"),
        pl.col("salto_atipico").fill_null(False).filter(pl.col("fecha") > fecha - timedelta(days=365)).any()
        .alias("salto_atipico_12m"),
    ))
    liq = pl.DataFrame(schema={"especie": pl.Utf8})
    if liquidez is not None:
        liq = (liquidez.filter(pl.col("fecha") <= fecha).sort("fecha").group_by("especie").agg(
            pl.col("monto_medio_60").last(), pl.col("fraccion_dias_60").last(), pl.col("liquida").last()))

    base = (activos.filter(pl.col("activa").fill_null(False) | pl.col("especie").is_in(ancho.get_column("especie").to_list()))
            .select("especie", "nombre", "emisor_id", "clase_accion", "sector_sfc"))
    t = (base.join(negocios, on="especie", how="left").join(liq, on="especie", how="left")
         .join(ancho, on="especie", how="left").join(actual, on="especie", how="left")
         .join(_fundamentales_a(fecha, activos), on="especie", how="left"))

    en_universo = pl.col("momentum_12_1_valor").is_not_null() if "momentum_12_1_valor" in t.columns \
        else pl.lit(False)
    t = t.with_columns(
        pl.lit(fecha).alias("fecha"),
        en_universo.alias("en_universo"),
        pl.col("puntaje").is_not_null().alias("con_puntaje"),
    )
    # Motivo de quedar fuera, en el orden en que la regla del universo lo descarta
    max_sin = int(u["max_sesiones_sin_negocio"])
    hist_min = int(u["sesiones_historia_min"])
    t = t.with_columns(
        pl.when(pl.col("en_universo")).then(None)
        .when(pl.col("fecha_ultimo_negocio").is_null()).then(pl.lit("sin negocios registrados"))
        .when((pl.lit(fecha) - pl.col("fecha_ultimo_negocio")).dt.total_days() > max_sin * 7 // 5 + 4)
        .then(pl.lit(f"sin negocio en las últimas {max_sin} sesiones"))
        .when(pl.col("dias_con_negocio") < hist_min).then(pl.lit(f"menos de {hist_min} días con negocios"))
        .when(~pl.col("liquida").fill_null(False)).then(pl.lit("no cumple el umbral de liquidez"))
        .otherwise(pl.lit("le falta un factor")).alias("motivo_fuera"),
    )
    # Retorno de 12 meses del índice limpio: (1 + momentum 12-1) × (1 + último mes) - 1
    if {"momentum_12_1_valor", "reversion_1m_valor"} <= set(t.columns):
        t = t.with_columns(((1 + pl.col("momentum_12_1_valor")) * (1 + pl.col("reversion_1m_valor")) - 1)
                           .alias("retorno_12m"),
                           pl.col("reversion_1m_valor").alias("retorno_1m"))
    t = t.with_columns(
        pl.when(pl.col("unidad_por_accion") == "pesos_por_accion")
        .then(pl.col("precio") / pl.col("utilidad_accion_anual")).otherwise(None).alias("precio_utilidad"),
        pl.when(pl.col("unidad_por_accion") == "pesos_por_accion")
        .then(pl.col("precio") / pl.col("valor_patrimonial_accion")).otherwise(None).alias("precio_valor_libros"),
    )
    # Con utilidad negativa la razón precio/utilidad no tiene lectura
    t = t.with_columns(pl.when(pl.col("precio_utilidad") > 0).then(pl.col("precio_utilidad"))
                       .otherwise(None).alias("precio_utilidad"))
    return _alertas(t, fecha).sort(["con_puntaje", "posicion", "nombre"], descending=[True, False, False],
                                   nulls_last=True)


def _alertas(t: pl.DataFrame, fecha: date) -> pl.DataFrame:
    """Avisos de calidad de datos, separados por "; " (vacío = sin avisos)."""
    avisos = [
        (pl.col("fraccion_dias_60").fill_null(0) < 0.5, "negocia menos de la mitad de los días"),
        (pl.col("salto_atipico_12m").fill_null(False), "salto atípico sin ajustar en el último año"),
        (pl.col("en_universo") & ((pl.lit(fecha) - pl.col("fecha_ultimo_negocio")).dt.total_days() > 7),
         "último negocio hace más de una semana"),
        (pl.col("corte_valor_patrimonial").is_not_null()
         & ((pl.lit(fecha) - pl.col("corte_valor_patrimonial")).dt.total_days() > 460),
         "valor patrimonial de hace más de 15 meses"),
        (pl.col("unidad_por_accion").fill_null("") == "usd_por_accion", "estados en dólares"),
    ]
    return t.with_columns(
        pl.concat_str([pl.when(c).then(pl.lit(m)).otherwise(None) for c, m in avisos], separator="; ",
                      ignore_nulls=True).alias("alertas"))


def construir() -> tuple[dict[str, pl.DataFrame], dict]:
    """Escribe las tres tablas gold. Necesita gold/fact_factor_mensual y gold/fact_validacion_factor."""
    cfg = config_factores()
    factores = storage.leer_tabla("gold", "fact_factor_mensual")
    validacion = storage.leer_tabla("gold", "fact_validacion_factor")
    validados = factores_validados(validacion)
    if factores is None or factores.is_empty() or not validados:
        return {}, {"validados": len(validados)}
    p = puntajes(factores, validados, cfg)
    v = cfg["validacion"]
    h = historia_tercio(p, date.fromisoformat(str(v["inicio"])), float(v.get("costo_ida", 0.0)),
                        int(cfg["universo"]["minimo_acciones_por_mes"]), int(v["cuantiles"]))
    f = ficha(p, factores, validados, cfg=cfg)
    storage.escribir_completa("gold", "fact_puntaje_mensual", p)
    if not h.is_empty():
        storage.escribir_completa("gold", "fact_puntaje_tercio", h)
    storage.escribir_completa("gold", "fact_ficha_accion", f)
    ultimo = f.get_column("fecha").max() if not f.is_empty() else None
    resumen = {
        "validados": len(validados), "fecha": ultimo,
        "con_puntaje": f.filter(pl.col("con_puntaje")).height if not f.is_empty() else 0,
        "fuera_del_universo": f.filter(~pl.col("en_universo")).height if not f.is_empty() else 0,
        "meses_historia": h.height,
    }
    return {"puntajes": p, "historia": h, "ficha": f}, resumen

