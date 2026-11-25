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
  ``apalancamiento`` (pasivos / activos) y ``efectivo_activos``; y, salvo bancos, aseguradoras y holdings
  financieros (``valoracion_empresa.excluir_financieros`` en config/convenciones.yaml), utilidad
  operacional y ``ebitda_12m`` (utilidad operacional + depreciación y amortización del estado de flujos),
  ``margen_bruto_12m``, ``margen_operacional_12m``, ``margen_ebitda_12m``, ``deuda_financiera``,
  ``deuda_neta`` (menos el efectivo), ``deuda_neta_ebitda`` e ``interes_minoritario``.
  Las cifras de resultados de los informes son acumuladas del año; las de 12 meses se arman como
  acumulado del año + año anterior completo − acumulado del mismo trimestre del año anterior.
- ``gold/fact_valoracion`` (especie × fin de mes, y la última sesión): precio, capitalización,
  ``p_vl``, ``p_u``, valor de la empresa (``ev``: capitalización de todas las clases del emisor + deuda
  neta + interés minoritario) y ``ev_ebitda``, rendimiento de la utilidad, rendimiento por dividendo de 12 meses (dividendos en
  efectivo con fecha ex en los últimos 365 días sobre el precio) y el ROE conocido a esa fecha. Si el
  emisor reporta en dólares, sus cifras por acción se pasan a pesos con la TRM del día.

Los bancos tienen además el CUIF mensual (silver/fact_cuif_mensual); sus métricas propias (ROTE,
margen de intereses, eficiencia, costo del riesgo, calidad de cartera) las arma la plantilla de bancos
(src/invest/bancos.py → gold/fact_metrica_banco).
"""

from __future__ import annotations

from datetime import timedelta

import polars as pl

from invest import storage
from invest.config import convenciones
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


def _suma(*partes: pl.DataFrame) -> pl.DataFrame:
    """Suma por emisor y corte de las partes que existan (una parte ausente cuenta como cero)."""
    partes = [p.with_columns(pl.col("fecha_corte").dt.month_end()) for p in partes if not p.is_empty()]
    if not partes:
        return pl.DataFrame(schema={k: ESQUEMA[k] for k in ("emisor_id", "fecha_corte", "fecha_disponible", "valor", "unidad")})
    return (pl.concat([p.select("emisor_id", "fecha_corte", "fecha_disponible", "valor", "unidad") for p in partes])
            .group_by("emisor_id", "fecha_corte")
            .agg(pl.col("fecha_disponible").max(), pl.col("valor").sum(), pl.col("unidad").first()))


def _resta(a: pl.DataFrame, b: pl.DataFrame, signo: float = -1.0) -> pl.DataFrame:
    """a − b (o a + b con ``signo=1``) por emisor y corte, solo donde existen los dos."""
    t = a.with_columns(pl.col("fecha_corte").dt.month_end()).join(
        b.with_columns(pl.col("fecha_corte").dt.month_end()).select(
            "emisor_id", "fecha_corte", pl.col("valor").alias("_b"), pl.col("fecha_disponible").alias("_fd")),
        on=["emisor_id", "fecha_corte"])
    return t.with_columns((pl.col("valor") + signo * pl.col("_b")).alias("valor"),
                          pl.max_horizontal("fecha_disponible", "_fd").alias("fecha_disponible")).select(
        "emisor_id", "fecha_corte", "fecha_disponible", "valor", "unidad")


def financieros() -> list[str]:
    return list(convenciones().get("valoracion_empresa", {}).get("excluir_financieros", []))


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


# Una utilidad de 12 meses que incluye una escisión o un reparto de acciones de otra sociedad trae la
# ganancia contable de la operación (Cementos Argos 2025: UPA de 2.084 contra 155 el año anterior) y
# su comparación con el año anterior cruza un cambio de estructura. Esas métricas quedan vacías (la
# fila se conserva, con valor nulo, para que el as-of no recupere un corte más viejo):
#   - las de 12 meses, si el evento cae en los 12 meses del corte;
#   - los crecimientos, si cae en los 24 meses que comparan.
METRICAS_DOCE_MESES = ("upa_12m", "roe_12m", "utilidad_controladora_12m", "roe_consolidado_12m", "roe_anual",
                       "margen_neto_12m", "payout")
METRICAS_CRECIMIENTO = ("crecimiento_upa_12m", "crecimiento_ingresos_12m")


def eventos_estructura() -> pl.DataFrame:
    """Escisiones y repartos de acciones de otra sociedad (``config/eventos_capital.yaml``) y sucesiones
    (``config/sucesiones.yaml``) por emisor."""
    from invest import distribuciones

    from invest import sucesiones

    filas = [{"emisor_id": "CO-" + str(e["especie"])[:5].upper(), "fecha_evento": distribuciones._fecha(e["fecha_ex"])}
             for e in (distribuciones.config().get("eventos") or [])]
    # Una sucesora (Grupo Cibest, mayo de 2025) reporta su primer año desde que existe: su utilidad de ese
    # año no es de 12 meses (Cibest 2025: 3.886 por acción, frente a unos 6.300 de Bancolombia en 2024)
    filas += [{"emisor_id": "CO-" + str(e["especie"])[:5].upper(), "fecha_evento": distribuciones._fecha(e["ultimo_dia"])}
              for e in sucesiones.cargar()]
    if not filas:
        return pl.DataFrame(schema={"emisor_id": pl.String, "fecha_evento": pl.Date})
    return pl.DataFrame(filas).with_columns(pl.col("fecha_evento").cast(pl.Date)).unique()


def sin_eventos_de_estructura(df: pl.DataFrame, eventos: pl.DataFrame | None = None) -> pl.DataFrame:
    """Anula (no borra) las métricas de 12 meses y de crecimiento cuyo periodo incluye un evento."""
    eventos = eventos if eventos is not None else eventos_estructura()
    if df.is_empty() or eventos.is_empty():
        return df
    meses = (pl.when(pl.col("metrica").is_in(METRICAS_CRECIMIENTO)).then(24)
             .when(pl.col("metrica").is_in(METRICAS_DOCE_MESES)).then(12).otherwise(None))
    marcadas = (df.with_row_index("_i").with_columns(meses.alias("_meses")).filter(pl.col("_meses").is_not_null())
                .join(eventos, on="emisor_id")
                .filter((pl.col("fecha_evento") <= pl.col("fecha_corte"))
                        & (pl.col("fecha_evento") > pl.col("fecha_corte").dt.offset_by("-1y"))
                        | (pl.col("_meses") == 24) & (pl.col("fecha_evento") <= pl.col("fecha_corte"))
                        & (pl.col("fecha_evento") > pl.col("fecha_corte").dt.offset_by("-2y")))
                .get_column("_i").unique())
    if marcadas.is_empty():
        return df
    return (df.with_row_index("_i").with_columns(
        pl.when(pl.col("_i").is_in(marcadas.implode())).then(None).otherwise(pl.col("valor")).alias("valor"))
        .drop("_i"))


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
    # Valor de la empresa: solo sector real (ver docstring)
    excluir = financieros()
    real = lambda c: con(c).filter(~pl.col("emisor_id").is_in(excluir))  # noqa: E731
    ingresos_r12 = ingresos_c12.filter(~pl.col("emisor_id").is_in(excluir))
    uop12 = doce_meses(real("utilidad_operacional"))
    ebitda12 = _resta(uop12, doce_meses(real("depreciacion_amortizacion")), signo=1.0)
    deuda = _suma(real("obligaciones_financieras_corrientes"), real("obligaciones_financieras_no_corrientes"),
                  real("titulos_emitidos"), real("titulos_emitidos_corrientes"), real("titulos_emitidos_no_corrientes"))
    deuda_neta = _resta(deuda, real("efectivo"))
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
        ("consolidado", "utilidad_operacional_12m"): uop12,
        ("consolidado", "ebitda_12m"): ebitda12,
        ("consolidado", "margen_bruto_12m"): _cociente(doce_meses(real("utilidad_bruta")), ingresos_r12),
        ("consolidado", "margen_operacional_12m"): _cociente(uop12, ingresos_r12),
        ("consolidado", "margen_ebitda_12m"): _cociente(ebitda12, ingresos_r12),
        ("consolidado", "deuda_financiera"): deuda,
        ("consolidado", "deuda_neta"): deuda_neta,
        ("consolidado", "deuda_neta_ebitda"): _cociente(deuda_neta, ebitda12),
        ("consolidado", "interes_minoritario"): _resta(real("patrimonio_total"), real("patrimonio_controladora")),
    }
    filas = [df.with_columns(pl.lit(base).alias("base"), pl.lit(m).alias("metrica"),
                             pl.col("fecha_corte").dt.month_end())
             for (base, m), df in partes.items() if not df.is_empty()]
    if not filas:
        return pl.DataFrame(schema=ESQUEMA)
    return sin_eventos_de_estructura(pl.concat([f.select(list(ESQUEMA)).cast(ESQUEMA) for f in filas])).sort(
        ["emisor_id", "fecha_corte", "base", "metrica"])


def fechas_valoracion(fechas: pl.Series) -> list:
    """Fechas de ``fact_valoracion``: la última sesión de cada mes y la última fecha con precio."""
    fin = fechas.max()
    meses = fines_de_mes(fechas.min(), fin)
    if not meses or meses[-1] < fin:
        meses.append(fin)
    return meses


def por_accion_asof(v: pl.DataFrame, metricas: pl.DataFrame,
                    nombres: tuple[str, ...] = ("vpa", "upa_12m", "roe_12m")) -> pl.DataFrame:
    """Agrega a ``v`` (columnas ``fecha``, ``emisor_id`` y ``trm``) las métricas separadas por acción
    conocidas a cada fecha (as-of por ``fecha_disponible``), con su corte en ``_corte_<métrica>``.

    Más de 15 meses (460 días) desde el corte ya no describe a la empresa: queda nulo. Las cifras por
    acción en dólares se pasan a pesos con la TRM del día. Las preferenciales usan las del emisor."""
    for alias in nombres:
        m = (metricas.filter((pl.col("metrica") == alias) & (pl.col("base") == "separado"))
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
    return v


def valor_empresa(v: pl.DataFrame, metricas: pl.DataFrame) -> pl.DataFrame:
    """Agrega a ``v`` (especie × fecha, con ``capitalizacion`` y ``trm``) el valor de la empresa del emisor y
    su EV/EBITDA, con las cifras consolidadas conocidas a cada fecha (as-of; más de 460 días: nulo).

    EV = capitalización de todas las clases del emisor con precio + deuda neta + interés minoritario.
    Montos en dólares (Canacol) se pasan a pesos con la TRM del día. Bancos y holdings financieros: nulo."""
    cap = v.group_by("emisor_id", "fecha").agg(pl.col("capitalizacion").sum().alias("_cap_emisor"),
                                                 pl.col("capitalizacion").is_null().any().alias("_cap_incompleta"))
    v = v.join(cap, on=["emisor_id", "fecha"], how="left")
    for alias in ("ebitda_12m", "deuda_neta", "interes_minoritario", "deuda_neta_ebitda"):
        m = (metricas.filter((pl.col("metrica") == alias) & (pl.col("base") == "consolidado")).sort("fecha_disponible")
             .select("emisor_id", "fecha_disponible", pl.col("valor").alias(alias), pl.col("unidad").alias(f"_u_{alias}"),
                     pl.col("fecha_corte").alias(f"_corte_{alias}")))
        v = v.sort("fecha").join_asof(m, left_on="fecha", right_on="fecha_disponible", by="emisor_id",
                                      strategy="backward", check_sortedness=False).drop("fecha_disponible")
        vigente = (pl.col("fecha") - pl.col(f"_corte_{alias}")).dt.total_days() <= 460
        en_pesos = pl.when(pl.col(f"_u_{alias}") == "usd").then(pl.col(alias) * pl.col("trm")).otherwise(pl.col(alias))
        v = v.with_columns(pl.when(vigente).then(en_pesos).otherwise(None).alias(alias))
    ev = pl.col("_cap_emisor") + pl.col("deuda_neta") + pl.col("interes_minoritario").fill_null(0.0)
    v = v.with_columns(pl.when(~pl.col("_cap_incompleta")).then(ev).otherwise(None).alias("ev"))
    return v.with_columns(
        pl.when(pl.col("ebitda_12m") > 0).then(pl.col("ev") / pl.col("ebitda_12m")).otherwise(None).alias("ev_ebitda"))


def fact_valoracion(metricas: pl.DataFrame | None = None) -> pl.DataFrame | None:
    precios = storage.leer_tabla("gold", "fact_precio_diario")
    if precios is None or precios.is_empty():
        return None
    metricas = metricas if metricas is not None else fact_metrica_fundamental()
    p = precios.filter(pl.col("precio").is_not_null()).sort(["especie", "fecha"])
    meses = fechas_valoracion(p.get_column("fecha"))
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
    v = por_accion_asof(v, metricas)
    v = valor_empresa(v, metricas)
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
        "rendimiento_dividendo_12m", "ev", "ebitda_12m", "ev_ebitda", "deuda_neta_ebitda",
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
