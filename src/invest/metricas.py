"""Métricas fundamentales por emisor y valoración por acción, sin mirar al futuro.

- ``gold/fact_metrica_fundamental`` (emisor × corte × métrica, formato largo): calculadas con
  ``gold/fact_fundamental``. Cada fila lleva su ``fecha_disponible`` (la más tardía de las cifras que
  usa), así que una consulta "lo que se sabía el día d" es ``fecha_disponible <= d``.
  Base **separada** (anexos de la SFC, por acción): valor patrimonial por acción (``vpa``), utilidad
  por acción de 12 meses (``upa_12m``), ``roe_12m`` (= upa_12m / vpa), dividendo por acción decretado,
  ``payout`` (dividendo decretado en el año sobre la utilidad por acción del año anterior) y crecimiento
  de la utilidad por acción en 12 meses. Los "ingresos" del anexo 141 no se usan: el cotejo con
  Supersociedades mostró que no son los ingresos operacionales. Base **separado_anual**
  (Supersociedades, respaldo de quienes no tienen XBRL): ingresos, ``apalancamiento``,
  ``margen_neto_12m``, ``roe_anual`` y ``crecimiento_ingresos_12m`` de cada cierre. Base **consolidada** (XBRL): utilidad de la
  controladora e ingresos de 12 meses, ``roe_consolidado_12m``, ``margen_neto_12m``,
  ``apalancamiento`` (pasivos / activos) y ``efectivo_activos``.
  Las cifras de resultados de los informes son acumuladas del año; las de 12 meses se arman como
  acumulado del año + año anterior completo − acumulado del mismo trimestre del año anterior.
- ``gold/fact_valoracion`` (especie × fin de mes, y la última sesión): precio, capitalización,
  ``p_vl``, ``p_u``, rendimiento de la utilidad, rendimiento por dividendo de 12 meses (dividendos en
  efectivo con fecha ex en los últimos 365 días sobre el precio) y el ROE conocido a esa fecha. Si el
  emisor reporta en dólares, sus cifras por acción se pasan a pesos con la TRM del día.

Los bancos tienen además el CUIF mensual (silver/fact_cuif_mensual); sus métricas propias (ROTE,
margen de intermediación, calidad de cartera) quedan para la plantilla de bancos.
"""

from __future__ import annotations

from datetime import timedelta

import polars as pl

from invest import storage
from invest.factores import fines_de_mes

ESQUEMA = {"emisor_id": pl.Utf8, "fecha_corte": pl.Date, "fecha_disponible": pl.Date, "base": pl.Utf8,
           "metrica": pl.Utf8, "valor": pl.Float64, "unidad": pl.Utf8}


def _serie(f: pl.DataFrame, concepto: str, base: str) -> pl.DataFrame:
    return (f.filter((pl.col("concepto") == concepto) & (pl.col("base") == base))
            .sort(["emisor_id", "fecha_corte", "fecha_disponible"])
            .unique(subset=["emisor_id", "fecha_corte"], keep="last", maintain_order=True)
            .select("emisor_id", "fecha_corte", "fecha_disponible", "valor", "unidad"))


def doce_meses(acumulado: pl.DataFrame) -> pl.DataFrame:
    """De cifras acumuladas del año a cifras de 12 meses (ver docstring del módulo)."""
    if acumulado.is_empty():
        return acumulado
    a = acumulado.with_columns(pl.col("fecha_corte").dt.year().alias("_anio"))
    dic = a.filter(pl.col("fecha_corte").dt.month() == 12).select(
        "emisor_id", (pl.col("_anio") + 1).alias("_anio"), pl.col("valor").alias("_dic"),
        pl.col("fecha_disponible").alias("_fd_dic"))
    previo = a.select("emisor_id", pl.col("fecha_corte").dt.offset_by("1y").dt.month_end().alias("fecha_corte"),
                      pl.col("valor").alias("_prev"), pl.col("fecha_disponible").alias("_fd_prev"))
    a = a.with_columns(pl.col("fecha_corte").dt.month_end())
    t = a.join(dic, on=["emisor_id", "_anio"], how="left").join(previo, on=["emisor_id", "fecha_corte"], how="left")
    return t.with_columns(
        pl.when(pl.col("fecha_corte").dt.month() == 12).then(pl.col("valor"))
        .otherwise(pl.col("valor") + pl.col("_dic") - pl.col("_prev")).alias("valor"),
        pl.when(pl.col("fecha_corte").dt.month() == 12).then(pl.col("fecha_disponible"))
        .otherwise(pl.max_horizontal("fecha_disponible", "_fd_dic", "_fd_prev")).alias("fecha_disponible"),
    ).filter(pl.col("valor").is_not_null()).select("emisor_id", "fecha_corte", "fecha_disponible", "valor", "unidad")


def _crecimiento(s: pl.DataFrame) -> pl.DataFrame:
    """Variación frente al mismo corte del año anterior (solo si el anterior es positivo)."""
    prev = s.select("emisor_id", pl.col("fecha_corte").dt.offset_by("1y").dt.month_end().alias("fecha_corte"),
                    pl.col("valor").alias("_p"), pl.col("fecha_disponible").alias("_fd"))
    return (s.with_columns(pl.col("fecha_corte").dt.month_end()).join(prev, on=["emisor_id", "fecha_corte"])
            .filter(pl.col("_p") > 0)
            .with_columns((pl.col("valor") / pl.col("_p") - 1).alias("valor"),
                          pl.max_horizontal("fecha_disponible", "_fd").alias("fecha_disponible"),
                          pl.lit("ratio").alias("unidad"))
            .select("emisor_id", "fecha_corte", "fecha_disponible", "valor", "unidad"))


def _cociente(num: pl.DataFrame, den: pl.DataFrame, solo_den_positivo: bool = True) -> pl.DataFrame:
    t = num.with_columns(pl.col("fecha_corte").dt.month_end()).join(
        den.with_columns(pl.col("fecha_corte").dt.month_end()).select(
            "emisor_id", "fecha_corte", pl.col("valor").alias("_d"), pl.col("fecha_disponible").alias("_fd")),
        on=["emisor_id", "fecha_corte"])
    if solo_den_positivo:
        t = t.filter(pl.col("_d") > 0)
    return t.with_columns((pl.col("valor") / pl.col("_d")).alias("valor"),
                          pl.max_horizontal("fecha_disponible", "_fd").alias("fecha_disponible"),
                          pl.lit("ratio").alias("unidad")).select("emisor_id", "fecha_corte", "fecha_disponible",
                                                                  "valor", "unidad")


def _payout(dividendo: pl.DataFrame, upa_anual: pl.DataFrame) -> pl.DataFrame:
    """Dividendo decretado en el año t sobre la utilidad por acción del cierre del año t-1."""
    cierre = upa_anual.filter(pl.col("fecha_corte").dt.month() == 12).select(
        "emisor_id", (pl.col("fecha_corte").dt.year() + 1).alias("_anio"), pl.col("valor").alias("_u"),
        pl.col("fecha_disponible").alias("_fd"))
    return (dividendo.with_columns(pl.col("fecha_corte").dt.year().alias("_anio"))
            .join(cierre, on=["emisor_id", "_anio"]).filter(pl.col("_u") > 0)
            .with_columns((pl.col("valor") / pl.col("_u")).alias("valor"),
                          pl.max_horizontal("fecha_disponible", "_fd").alias("fecha_disponible"),
                          pl.lit("ratio").alias("unidad"))
            .select("emisor_id", "fecha_corte", "fecha_disponible", "valor", "unidad"))


def fact_metrica_fundamental(fund: pl.DataFrame | None = None) -> pl.DataFrame:
    fund = fund if fund is not None else storage.leer_tabla("gold", "fact_fundamental")
    if fund is None or fund.is_empty():
        return pl.DataFrame(schema=ESQUEMA)
    sep = lambda c: _serie(fund, c, "separado")  # noqa: E731
    con = lambda c: _serie(fund, c, "consolidado")  # noqa: E731
    vpa, upa = sep("valor_patrimonial_accion"), sep("utilidad_accion")
    upa12 = doce_meses(upa)
    div = sep("dividendo_accion_ordinaria")
    anual = lambda c: _serie(fund, c, "separado_anual")  # noqa: E731
    ingresos_a = anual("ingresos_operacionales")
    util_ctrl12 = doce_meses(con("utilidad_controladora"))
    util_tot12 = doce_meses(con("utilidad_total"))
    ingresos_c12 = doce_meses(con("ingresos_consolidados"))
    partes = {
        ("separado", "vpa"): vpa,
        ("separado", "upa_12m"): upa12,
        ("separado", "roe_12m"): _cociente(upa12, vpa),
        ("separado", "dividendo_accion"): div,
        ("separado", "payout"): _payout(div, upa),
        ("separado", "crecimiento_upa_12m"): _crecimiento(upa12),
        ("separado_anual", "ingresos_12m"): ingresos_a,
        ("separado_anual", "crecimiento_ingresos_12m"): _crecimiento(ingresos_a),
        ("separado_anual", "margen_neto_12m"): _cociente(anual("utilidad_total"), ingresos_a),
        ("separado_anual", "roe_anual"): _cociente(anual("utilidad_total"), anual("patrimonio_total")),
        ("separado_anual", "apalancamiento"): _cociente(anual("pasivos"), anual("activos")),
        ("consolidado", "utilidad_controladora_12m"): util_ctrl12,
        ("consolidado", "ingresos_12m"): ingresos_c12,
        ("consolidado", "roe_consolidado_12m"): _cociente(util_ctrl12, con("patrimonio_controladora")),
        ("consolidado", "margen_neto_12m"): _cociente(util_tot12, ingresos_c12),
        ("consolidado", "apalancamiento"): _cociente(con("pasivos"), con("activos")),
        ("consolidado", "efectivo_activos"): _cociente(con("efectivo"), con("activos")),
        ("consolidado", "crecimiento_ingresos_12m"): _crecimiento(ingresos_c12),
    }
    filas = [df.with_columns(pl.lit(base).alias("base"), pl.lit(m).alias("metrica"),
                             pl.col("fecha_corte").dt.month_end())
             for (base, m), df in partes.items() if not df.is_empty()]
    if not filas:
        return pl.DataFrame(schema=ESQUEMA)
    return pl.concat([f.select(list(ESQUEMA)).cast(ESQUEMA) for f in filas]).sort(
        ["emisor_id", "fecha_corte", "base", "metrica"])


def fact_valoracion(metricas: pl.DataFrame | None = None) -> pl.DataFrame | None:
    precios = storage.leer_tabla("gold", "fact_precio_diario")
    if precios is None or precios.is_empty():
        return None
    metricas = metricas if metricas is not None else fact_metrica_fundamental()
    p = precios.filter(pl.col("precio").is_not_null()).sort(["especie", "fecha"])
    fin = p.get_column("fecha").max()
    meses = fines_de_mes(p.get_column("fecha").min(), fin)
    if not meses or meses[-1] < fin:
        meses.append(fin)
    grilla = (p.select("especie", "emisor_id").unique()
              .join(pl.DataFrame({"fecha": meses}), how="cross").sort("fecha"))
    ultimos = p.select("fecha", "especie", pl.col("fecha").alias("fecha_precio"), "precio", "capitalizacion",
                       "capitalizacion_usd", "trm")
    v = grilla.join_asof(ultimos, on="fecha", by="especie", strategy="backward", check_sortedness=False)
    v = v.filter(pl.col("fecha_precio").is_not_null()
                 & ((pl.col("fecha") - pl.col("fecha_precio")).dt.total_days() <= 30))
    # Dividendos en efectivo con fecha ex en los últimos 365 días
    div = p.filter(pl.col("dividendo").fill_null(0) > 0).select("especie", pl.col("fecha").alias("_fex"), "dividendo")
    suma = (v.select("especie", "fecha").join(div, on="especie")
            .filter((pl.col("_fex") <= pl.col("fecha")) & (pl.col("_fex") > pl.col("fecha") - timedelta(days=365)))
            .group_by("especie", "fecha").agg(pl.col("dividendo").sum().alias("_div12")))
    v = v.join(suma, on=["especie", "fecha"], how="left")
    # Métricas por acción conocidas a la fecha (separadas), as-of por fecha_disponible
    for metrica, alias in (("vpa", "vpa"), ("upa_12m", "upa_12m"), ("roe_12m", "roe_12m")):
        m = (metricas.filter((pl.col("metrica") == metrica) & (pl.col("base") == "separado"))
             .sort("fecha_disponible")
             .select("emisor_id", "fecha_disponible", pl.col("valor").alias(alias),
                     pl.col("unidad").alias(f"_u_{alias}"), pl.col("fecha_corte").alias(f"_corte_{alias}")))
        v = v.sort("fecha").join_asof(m, left_on="fecha", right_on="fecha_disponible", by="emisor_id",
                                      strategy="backward", check_sortedness=False).drop("fecha_disponible")
        # Más de 15 meses sin actualizar ya no describe a la empresa
        v = v.with_columns(pl.when((pl.col("fecha") - pl.col(f"_corte_{alias}")).dt.total_days() > 460)
                           .then(None).otherwise(pl.col(alias)).alias(alias))
        if alias != "roe_12m":
            v = v.with_columns(pl.when(pl.col(f"_u_{alias}") == "usd_por_accion").then(pl.col(alias) * pl.col("trm"))
                               .otherwise(pl.col(alias)).alias(alias))
    v = v.with_columns(
        (pl.col("precio") / pl.col("vpa")).alias("p_vl"),
        pl.when(pl.col("upa_12m") > 0).then(pl.col("precio") / pl.col("upa_12m")).otherwise(None).alias("p_u"),
        (pl.col("upa_12m") / pl.col("precio")).alias("rendimiento_utilidad"),
        (pl.col("_div12").fill_null(0.0) / pl.col("precio")).alias("rendimiento_dividendo_12m"),
        pl.col("_corte_vpa").alias("corte_vpa"), pl.col("_corte_upa_12m").alias("corte_upa"),
    )
    return v.select(
        "fecha", "especie", "emisor_id", "fecha_precio", "precio", "capitalizacion", "capitalizacion_usd",
        "vpa", "corte_vpa", "upa_12m", "corte_upa", "roe_12m", "p_vl", "p_u", "rendimiento_utilidad",
        "rendimiento_dividendo_12m",
    ).sort(["especie", "fecha"])


def construir() -> dict:
    metricas = fact_metrica_fundamental()
    resumen = {}
    if not metricas.is_empty():
        storage.escribir_completa("gold", "fact_metrica_fundamental", metricas)
        resumen["metricas"] = metricas.height
        resumen["emisores"] = metricas.get_column("emisor_id").n_unique()
    valoracion = fact_valoracion(metricas)
    if valoracion is not None:
        storage.escribir_por_anio("gold", "fact_valoracion", valoracion, "fecha")
        resumen["valoracion"] = valoracion.height
    return resumen
