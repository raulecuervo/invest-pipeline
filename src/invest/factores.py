"""Factores mensuales de las acciones y su validación estadística.

Todo se calcula con lo que se sabía al cierre de cada mes (sin sesgo de anticipación):

1. **Índice limpio** de retorno total por acción: se encadenan los retornos totales de los días con
   negocios, salvo los saltos atípicos (``salto_atipico``), que cuentan como 0. Así un precio viejo
   o un posible split sin confirmar no contamina el momentum ni la volatilidad.
2. **Fin de mes**: última sesión XBOG de cada mes. Para cada acción se toma el último dato a esa
   fecha (as-of) y se exige que haya negociado en las últimas ``max_sesiones_sin_negocio`` sesiones.
3. **Universo**: acción líquida a fin de mes (``gold/fact_liquidez_diaria``), con al menos
   ``sesiones_historia_min`` días con negocios.
4. **Factores** (``config/factores.yaml``): momentum 12-1 y 6-1, reversión de un mes, volatilidad de
   un año, liquidez y rendimiento por dividendo de 12 meses. Cada uno con su percentil dentro del
   universo del mes.
5. **Retorno siguiente**: retorno total desde este fin de mes hasta el siguiente, con el mismo índice.

Salidas: ``gold/fact_factor_mensual`` (fecha, especie, factor, valor, percentil, retorno siguiente)
y ``gold/fact_validacion_factor`` (ver ``validar``).

Validación (``validar``), por factor y aplicando su signo de hipótesis:

- IC mensual: correlación de Spearman entre el factor y el retorno del mes siguiente, en los meses
  con al menos ``minimo_acciones_por_mes`` acciones. Media, estadístico t de Newey-West (los IC de
  meses vecinos no son independientes) y valor p bilateral.
- Spread: retorno medio del tercio superior menos el del tercio inferior (pesos iguales), con su
  t de Newey-West y Sharpe anualizado.
- Muestra de descubrimiento (``inicio`` a ``inicio_fuera_de_muestra``) y fuera de muestra (después).
- Corrección de Holm sobre los valores p de la muestra completa, porque se prueban varios factores.
- Veredicto: ``validado`` si el p ajustado < ``nivel`` y el IC medio es positivo dentro y fuera de
  muestra; ``debil`` si el p sin ajustar < ``nivel_debil`` con el mismo signo en ambas muestras;
  ``no_validado`` en otro caso. Con 20 a 30 acciones líquidas el poder estadístico es bajo: lo
  esperable es que pocos o ningún factor se validen, y eso es un resultado, no un error.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl

from invest import storage
from invest.calendario import sesiones_entre
from invest.config import cargar_yaml

DIAS_ANIO = 252


def config() -> dict:
    return cargar_yaml("factores.yaml")


# --------------------------------------------------------------------------- utilidades
def fines_de_mes(desde: date, hasta: date) -> list[date]:
    """Última sesión XBOG de cada mes completo entre las dos fechas."""
    salida: dict[tuple[int, int], date] = {}
    for d in sesiones_entre(desde, hasta):
        salida[(d.year, d.month)] = d
    return sorted(salida.values())


def newey_west(serie: list[float], rezagos: int) -> tuple[float, float, float]:
    """(media, t, p bilateral) con error estándar de Newey-West (núcleo de Bartlett)."""
    x = [v for v in serie if v is not None and not math.isnan(v)]
    n = len(x)
    if n < 3:
        return (sum(x) / n if n else float("nan")), float("nan"), float("nan")
    m = sum(x) / n
    d = [v - m for v in x]
    var = sum(v * v for v in d) / n
    for k in range(1, min(rezagos, n - 1) + 1):
        gamma = sum(d[i] * d[i - k] for i in range(k, n)) / n
        var += 2 * (1 - k / (rezagos + 1)) * gamma
    if var <= 0:
        return m, float("nan"), float("nan")
    t = m / math.sqrt(var / n)
    p = math.erfc(abs(t) / math.sqrt(2))
    return m, t, p


def holm(pvalores: dict[str, float]) -> dict[str, float]:
    """Valores p ajustados por Holm-Bonferroni (los nan quedan nan)."""
    validos = sorted((p, k) for k, p in pvalores.items() if p == p)
    m, ajustados, maximo = len(validos), {}, 0.0
    for i, (p, k) in enumerate(validos):
        maximo = max(maximo, min(1.0, (m - i) * p))
        ajustados[k] = maximo
    return {k: ajustados.get(k, float("nan")) for k in pvalores}


def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra = pl.Series(a).rank("average").to_list()
    rb = pl.Series(b).rank("average").to_list()
    n = len(ra)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else None


# --------------------------------------------------------------------------- factores
def _indice_limpio(retornos: pl.DataFrame) -> pl.DataFrame:
    return (
        retornos.select("fecha", "especie", "precio_promedio", "dividendo", "retorno_total", "salto_atipico")
        .sort(["especie", "fecha"])
        .with_columns(
            pl.when(pl.col("salto_atipico").fill_null(False)).then(0.0)
            .otherwise(pl.col("retorno_total").fill_null(0.0)).alias("r"))
        .with_columns(
            (1 + pl.col("r")).cum_prod().over("especie").alias("indice"),
            pl.int_range(1, pl.len() + 1).over("especie").alias("n_negocios"),
        )
    )


def _asof(panel: pl.DataFrame, fechas: list[date], columnas: list[str]) -> pl.DataFrame:
    """Para cada acción y fecha, el último dato del panel a esa fecha (y la fecha de ese dato)."""
    especies = panel.select("especie").unique()
    grilla = especies.join(pl.DataFrame({"fecha_mes": fechas}), how="cross").sort(["especie", "fecha_mes"])
    datos = panel.select("especie", pl.col("fecha").alias("fecha_dato"), *columnas).sort(["especie", "fecha_dato"])
    return grilla.join_asof(datos, left_on="fecha_mes", right_on="fecha_dato", by="especie", strategy="backward",
                           check_sortedness=False)


def _valor_libros_mes(meses: list[date]) -> pl.DataFrame:
    """Valor patrimonial por acción conocido a cada fin de mes (``fecha_disponible`` ≤ fin de mes), por especie.

    Sale de ``gold/fact_fundamental`` (anexos de la SFC, estados separados, en pesos). Las especies
    ordinaria y preferencial de un emisor comparten el valor patrimonial por acción.
    """
    vacio = pl.DataFrame(schema={"especie": pl.Utf8, "fecha_mes": pl.Date, "_vp": pl.Float64})
    fund = storage.leer_tabla("gold", "fact_fundamental")
    activos = storage.leer_dim("dim_activo")
    if fund is None or activos is None or fund.is_empty():
        return vacio
    vp = (fund.filter((pl.col("concepto") == "valor_patrimonial_accion") & (pl.col("unidad") == "pesos_por_accion"))
          .select("emisor_id", "fecha_disponible", "fecha_corte", pl.col("valor").alias("_vp"))
          .sort(["emisor_id", "fecha_disponible", "fecha_corte"])
          .unique(subset=["emisor_id", "fecha_disponible"], keep="last", maintain_order=True))
    if vp.is_empty():
        return vacio
    grilla = (vp.select("emisor_id").unique().join(pl.DataFrame({"fecha_mes": meses}), how="cross")
              .sort(["emisor_id", "fecha_mes"]))
    por_emisor = grilla.join_asof(vp.drop("fecha_corte"), left_on="fecha_mes", right_on="fecha_disponible",
                                  by="emisor_id", strategy="backward", check_sortedness=False)
    # Un valor patrimonial de hace más de 15 meses ya no describe a la empresa
    por_emisor = por_emisor.filter(pl.col("_vp").is_not_null()
                                   & ((pl.col("fecha_mes") - pl.col("fecha_disponible")).dt.total_days() <= 460))
    return (por_emisor.join(activos.select("especie", "emisor_id"), on="emisor_id")
            .select("especie", "fecha_mes", "_vp"))


def _utilidad_mes(meses: list[date]) -> pl.DataFrame:
    """Utilidad por acción de 12 meses conocida a cada fin de mes, por especie (``gold/fact_metrica_fundamental``,
    estados separados, en pesos; a lo sumo 15 meses después de quedar disponible, como el valor patrimonial)."""
    vacio = pl.DataFrame(schema={"especie": pl.Utf8, "fecha_mes": pl.Date, "_upa": pl.Float64})
    met = storage.leer_tabla("gold", "fact_metrica_fundamental")
    activos = storage.leer_dim("dim_activo")
    if met is None or activos is None or met.is_empty():
        return vacio
    u = (met.filter((pl.col("metrica") == "upa_12m") & (pl.col("unidad") == "pesos_por_accion")
                    & pl.col("valor").is_not_null())
         .select("emisor_id", "fecha_disponible", "fecha_corte", pl.col("valor").alias("_upa"))
         .sort(["emisor_id", "fecha_disponible", "fecha_corte"])
         .unique(subset=["emisor_id", "fecha_disponible"], keep="last", maintain_order=True))
    if u.is_empty():
        return vacio
    grilla = (u.select("emisor_id").unique().join(pl.DataFrame({"fecha_mes": meses}), how="cross")
              .sort(["emisor_id", "fecha_mes"]))
    por_emisor = grilla.join_asof(u.drop("fecha_corte"), left_on="fecha_mes", right_on="fecha_disponible",
                                  by="emisor_id", strategy="backward", check_sortedness=False)
    por_emisor = por_emisor.filter(pl.col("_upa").is_not_null()
                                   & ((pl.col("fecha_mes") - pl.col("fecha_disponible")).dt.total_days() <= 460))
    return (por_emisor.join(activos.select("especie", "emisor_id"), on="emisor_id")
            .select("especie", "fecha_mes", "_upa"))


def construir(hoy: date | None = None) -> tuple[pl.DataFrame | None, dict]:
    cfg = config()
    retornos = storage.leer_tabla("gold", "fact_retorno_diario")
    liquidez = storage.leer_tabla("gold", "fact_liquidez_diaria")
    if retornos is None or retornos.is_empty() or liquidez is None:
        return None, {}
    u = cfg["universo"]
    panel = _indice_limpio(retornos)
    inicio = panel.get_column("fecha").min()
    fin = panel.get_column("fecha").max()
    meses = fines_de_mes(inicio, fin)
    if len(meses) < 14:
        return None, {"meses": len(meses)}
    sesion_idx = {d: i for i, d in enumerate(sesiones_entre(inicio, fin))}

    base = _asof(panel, meses, ["indice", "n_negocios", "precio_promedio"])
    base = base.with_columns(
        pl.col("fecha_dato").map_elements(lambda d: sesion_idx.get(d), return_dtype=pl.Int64).alias("_i_dato"),
        pl.col("fecha_mes").map_elements(lambda d: sesion_idx.get(d), return_dtype=pl.Int64).alias("_i_mes"),
    ).with_columns((pl.col("_i_mes") - pl.col("_i_dato")).alias("sesiones_sin_negocio"))
    base = base.sort(["especie", "fecha_mes"]).with_columns(
        pl.col("indice").shift(1).over("especie").alias("i_m1"),
        pl.col("indice").shift(6).over("especie").alias("i_m6"),
        pl.col("indice").shift(12).over("especie").alias("i_m12"),
        pl.col("indice").shift(-1).over("especie").alias("i_sig"),
        pl.col("sesiones_sin_negocio").shift(-1).over("especie").alias("_sin_neg_sig"),
    )

    # Volatilidad de un año y dividendos de 12 meses, con datos hasta cada fin de mes
    obs_min = int(cfg["factores"].get("volatilidad_1a", {}).get("observaciones_min", 100))
    diarios = panel.filter(~pl.col("salto_atipico").fill_null(False)).select(
        "especie", "fecha", "r",
        (pl.col("dividendo").fill_null(0.0) / (pl.col("precio_promedio") + pl.col("dividendo").fill_null(0.0)))
        .alias("rd"))
    ventanas = []
    for m in meses:
        v = (diarios.filter((pl.col("fecha") > m - timedelta(days=365)) & (pl.col("fecha") <= m))
             .group_by("especie").agg(
                 pl.col("r").std().alias("_sd"), pl.len().alias("_n"), pl.col("rd").sum().alias("rendimiento_dividendo_12m"))
             .with_columns(pl.lit(m).alias("fecha_mes")))
        ventanas.append(v)
    ventanas = pl.concat(ventanas).with_columns(
        pl.when(pl.col("_n") >= obs_min).then(pl.col("_sd") * math.sqrt(DIAS_ANIO)).otherwise(None)
        .alias("volatilidad_1a")).drop("_sd", "_n")

    liq = liquidez.select("especie", "fecha", "monto_medio_60", "liquida").sort(["especie", "fecha"])
    liq_mes = _asof(liq, meses, ["monto_medio_60", "liquida"]).drop("fecha_dato")

    valor = _valor_libros_mes(meses)
    utilidad = _utilidad_mes(meses)
    df = (
        base.join(ventanas, on=["especie", "fecha_mes"], how="left")
        .join(liq_mes, on=["especie", "fecha_mes"], how="left")
        .join(valor, on=["especie", "fecha_mes"], how="left")
        .join(utilidad, on=["especie", "fecha_mes"], how="left")
        .with_columns((pl.col("_vp") / pl.col("precio_promedio")).alias("valor_libros_precio"),
                      (pl.col("_upa") / pl.col("precio_promedio")).alias("rendimiento_utilidad_12m"))
        .with_columns(
            (pl.col("i_m1") / pl.col("i_m12") - 1).alias("momentum_12_1"),
            (pl.col("i_m1") / pl.col("i_m6") - 1).alias("momentum_6_1"),
            (pl.col("indice") / pl.col("i_m1") - 1).alias("reversion_1m"),
            pl.when(pl.col("monto_medio_60") > 0).then(pl.col("monto_medio_60").log()).otherwise(None).alias("liquidez_60"),
            pl.when(pl.col("_sin_neg_sig") <= int(u["max_sesiones_sin_negocio"]))
            .then(pl.col("i_sig") / pl.col("indice") - 1).otherwise(None).alias("retorno_siguiente"),
        )
    )
    en_universo = (
        pl.col("fecha_dato").is_not_null()
        & (pl.col("sesiones_sin_negocio") <= int(u["max_sesiones_sin_negocio"]))
        & (pl.col("n_negocios") >= int(u["sesiones_historia_min"]))
    )
    if u.get("requiere_liquida", True):
        en_universo = en_universo & pl.col("liquida").fill_null(False)
    df = df.filter(en_universo)

    nombres = list(cfg["factores"])
    largo = (
        df.select("fecha_mes", "especie", "retorno_siguiente", *nombres)
        .unpivot(index=["fecha_mes", "especie", "retorno_siguiente"], on=nombres, variable_name="factor", value_name="valor")
        .filter(pl.col("valor").is_not_null() & pl.col("valor").is_finite())
        .with_columns(
            ((pl.col("valor").rank("average").over(["fecha_mes", "factor"]) - 0.5)
             / pl.len().over(["fecha_mes", "factor"])).alias("percentil"),
            pl.len().over(["fecha_mes", "factor"]).alias("acciones_mes"),
        )
        .rename({"fecha_mes": "fecha"})
        .sort(["factor", "fecha", "especie"])
    )
    storage.escribir_completa("gold", "fact_factor_mensual", largo)
    resumen = {
        "meses": largo.get_column("fecha").n_unique(),
        "acciones_por_mes_mediana": int(df.group_by("fecha_mes").len().get_column("len").median() or 0),
        "filas": largo.height,
    }
    return largo, resumen


# --------------------------------------------------------------------------- validación
def _metricas(sub: pl.DataFrame, signo: int, minimo: int, cuantiles: int, rezagos: int, costo_ida: float = 0.0) -> dict:
    """IC, spread largo-corto y, como en Colombia no se puede vender en corto con facilidad, el exceso
    del tercio superior sobre el promedio del universo, bruto y neto de costos (compra y venta de lo
    que entra y sale del tercio cada mes)."""
    ics, spreads, excesos, netos, rotaciones = [], [], [], [], []
    anterior: set | None = None
    # Orden fijo (fecha, especie): los empates del factor (p. ej. rendimiento por dividendo 0) se
    # desempatan siempre igual y el resultado no depende del orden en que llegan las filas
    for (_,), g in (sub.filter(pl.col("retorno_siguiente").is_not_null()).sort(["fecha", "especie"])
                    .group_by(["fecha"], maintain_order=True)):
        if g.height < minimo:
            continue
        ic = _spearman((g.get_column("valor") * signo).to_list(), g.get_column("retorno_siguiente").to_list())
        if ic is not None:
            ics.append(ic)
        # Grupo de cada acción (0 = tercio con menor valor según la hipótesis, cuantiles-1 = mayor)
        orden = (g.get_column("valor") * signo).rank("ordinal").to_list()
        grupos = [(o - 1) * cuantiles // g.height for o in orden]
        rets = g.get_column("retorno_siguiente").to_list()
        alto = [r for r, k in zip(rets, grupos) if k == cuantiles - 1]
        bajo = [r for r, k in zip(rets, grupos) if k == 0]
        if alto and bajo:
            spreads.append(sum(alto) / len(alto) - sum(bajo) / len(bajo))
        if alto:
            especies = g.get_column("especie").to_list()
            arriba = {e for e, k in zip(especies, grupos) if k == cuantiles - 1}
            rotacion = 1.0 if anterior is None else 1 - len(arriba & anterior) / len(arriba)
            exceso = sum(alto) / len(alto) - sum(rets) / len(rets)
            excesos.append(exceso)
            netos.append(exceso - 2 * costo_ida * rotacion)
            rotaciones.append(rotacion)
            anterior = arriba
    ic_m, ic_t, ic_p = newey_west(ics, rezagos)
    ex_m, ex_t, _ = newey_west(excesos, rezagos)
    ne_m, ne_t, _ = newey_west(netos, rezagos)
    sp_m, sp_t, _ = newey_west(spreads, rezagos)
    sd = (sum((s - sp_m) ** 2 for s in spreads) / (len(spreads) - 1)) ** 0.5 if len(spreads) > 1 else float("nan")
    return {
        "meses": len(ics), "ic_medio": ic_m, "ic_t": ic_t, "ic_p": ic_p,
        "ic_positivo": (sum(1 for i in ics if i > 0) / len(ics)) if ics else float("nan"),
        "spread_mensual": sp_m, "spread_t": sp_t,
        "spread_sharpe_anual": (sp_m / sd * math.sqrt(12)) if sd and sd == sd and sd > 0 else float("nan"),
        "rotacion_superior": (sum(rotaciones[1:]) / len(rotaciones[1:])) if len(rotaciones) > 1 else float("nan"),
        "exceso_superior": ex_m, "exceso_superior_t": ex_t,
        "exceso_superior_neto": ne_m, "exceso_superior_neto_t": ne_t,
    }


def validar(factores: pl.DataFrame | None = None) -> tuple[pl.DataFrame | None, dict]:
    cfg = config()
    factores = factores if factores is not None else storage.leer_tabla("gold", "fact_factor_mensual")
    if factores is None or factores.is_empty():
        return None, {}
    v = cfg["validacion"]
    inicio = date.fromisoformat(str(v["inicio"]))
    corte = date.fromisoformat(str(v["inicio_fuera_de_muestra"]))
    minimo = int(cfg["universo"]["minimo_acciones_por_mes"])
    q, L = int(v["cuantiles"]), int(v["rezagos_newey_west"])
    costo = float(v.get("costo_ida", 0.0))
    filas = []
    for nombre, f in cfg["factores"].items():
        sub = factores.filter((pl.col("factor") == nombre) & (pl.col("fecha") >= inicio))
        signo = int(f.get("signo", 1))
        total = _metricas(sub, signo, minimo, q, L, costo)
        dentro = _metricas(sub.filter(pl.col("fecha") < corte), signo, minimo, q, L)
        fuera = _metricas(sub.filter(pl.col("fecha") >= corte), signo, minimo, q, L)
        filas.append({"factor": nombre, "signo_hipotesis": signo, **total,
                      "ic_medio_descubrimiento": dentro["ic_medio"], "meses_descubrimiento": dentro["meses"],
                      "ic_medio_fuera_muestra": fuera["ic_medio"], "meses_fuera_muestra": fuera["meses"],
                      "spread_fuera_muestra": fuera["spread_mensual"]})
    ajustados = holm({r["factor"]: r["ic_p"] for r in filas})
    for r in filas:
        r["ic_p_holm"] = ajustados[r["factor"]]
        mismo_signo = (r["ic_medio_descubrimiento"] or 0) > 0 and (r["ic_medio_fuera_muestra"] or 0) > 0
        if r["ic_p_holm"] == r["ic_p_holm"] and r["ic_p_holm"] < float(v["nivel"]) and mismo_signo:
            r["veredicto"] = "validado"
        elif r["ic_p"] == r["ic_p"] and r["ic_p"] < float(v["nivel_debil"]) and mismo_signo:
            r["veredicto"] = "debil"
        else:
            r["veredicto"] = "no_validado"
    # Combinación de los validados (promedio de percentiles con su signo). Se elige después de ver los
    # resultados, así que no entra en la corrección de Holm ni puede quedar "validado": es informativa.
    validados = [r["factor"] for r in filas if r["veredicto"] == "validado"]
    if len(validados) >= 2:
        from invest.ficha import minimo_factores
        signos = {k: int(cfg["factores"][k].get("signo", 1)) for k in validados}
        comb = (factores.filter(pl.col("factor").is_in(validados) & (pl.col("fecha") >= inicio))
                .with_columns(pl.when(pl.col("factor").replace_strict(signos, default=1) > 0)
                              .then(pl.col("percentil")).otherwise(1 - pl.col("percentil")).alias("_p"))
                .group_by("fecha", "especie")
                .agg(pl.col("_p").mean().alias("valor"), pl.col("retorno_siguiente").first(), pl.len().alias("_n"))
                .filter(pl.col("_n") >= minimo_factores(len(validados), cfg)))
        total = _metricas(comb, 1, minimo, q, L, costo)
        dentro = _metricas(comb.filter(pl.col("fecha") < corte), 1, minimo, q, L, costo)
        fuera = _metricas(comb.filter(pl.col("fecha") >= corte), 1, minimo, q, L, costo)
        filas.append({"factor": "combinado_validados", "signo_hipotesis": 1, **total,
                      "ic_medio_descubrimiento": dentro["ic_medio"], "meses_descubrimiento": dentro["meses"],
                      "ic_medio_fuera_muestra": fuera["ic_medio"], "meses_fuera_muestra": fuera["meses"],
                      "spread_fuera_muestra": fuera["spread_mensual"], "ic_p_holm": float("nan"),
                      "veredicto": "informativo"})
    tabla = pl.DataFrame(filas).with_columns(pl.lit(date.today()).alias("fecha_calculo"))
    storage.escribir_completa("gold", "fact_validacion_factor", tabla)
    storage.escribir_completa("gold", "fact_correlacion_factor", correlaciones(factores, inicio))
    resumen = {k: tabla.filter(pl.col("veredicto") == k).height for k in ("validado", "debil", "no_validado")}
    return tabla, resumen


def correlaciones(factores: pl.DataFrame, inicio: date) -> pl.DataFrame:
    """Correlación de Spearman media, mes a mes, entre cada par de factores (cuánto se solapan)."""
    cfg = config()
    nombres = list(cfg["factores"])
    ancho = (factores.filter(pl.col("fecha") >= inicio).pivot(on="factor", index=["fecha", "especie"], values="valor"))
    filas = []
    for i, a in enumerate(nombres):
        for b in nombres[i + 1:]:
            if a not in ancho.columns or b not in ancho.columns:
                continue
            rs = []
            for (_,), g in ancho.select("fecha", a, b).drop_nulls().group_by(["fecha"]):
                if g.height >= int(cfg["universo"]["minimo_acciones_por_mes"]):
                    r = _spearman(g.get_column(a).to_list(), g.get_column(b).to_list())
                    if r is not None:
                        rs.append(r)
            if rs:
                filas.append({"factor_a": a, "factor_b": b, "correlacion_media": sum(rs) / len(rs), "meses": len(rs)})
    return pl.DataFrame(filas, schema={"factor_a": pl.Utf8, "factor_b": pl.Utf8, "correlacion_media": pl.Float64,
                                       "meses": pl.Int64})
