"""Retorno total y precios ajustados por dividendos.

Precio base: el precio promedio ponderado diario de la SFC (no hay cierre en la fuente oficial
gratuita). Solo días con negociación real: la SFC repite el último precio los días sin negocios
(valor transado 0) y esas filas se descartan, porque contarlas como retorno cero subestima la
volatilidad de las acciones poco líquidas. Entre dos días negociados el retorno cubre todo el
intervalo (columna ``dias_desde_anterior``).

Para cada especie y día negociado t, con P el precio y D el dividendo en efectivo por acción cuyo
periodo ex-dividendo empieza en t (o en el último día sin negociación antes de t):

    retorno_precio_t = P_t / P_(t-1) - 1
    retorno_total_t  = (P_t + D_t + E_t) / P_(t-1) - 1

con E el valor de lo entregado en especie ese día (escisiones: acciones de otra sociedad que el
accionista recibe sin dejar las suyas; ver ``distribuciones.py`` y config/eventos_capital.yaml).
    indice_rt_t      = 100 × Π (1 + retorno_total)          (hacia adelante, base 100 el primer día)
    precio_ajustado_t = indice_rt_t × P_último / indice_rt_último

Así, los retornos del precio ajustado son exactamente los retornos totales (el dividendo se
reinvierte al precio del primer día ex; factor P_t / (P_t + D_t)). Es distinto del ajuste estilo
Yahoo (1 - D / P_(t-1)), que no cuadra con el retorno total.

Se guarda el índice y no el precio ajustado: el índice no cambia hacia atrás cuando entra un
dividendo nuevo, así que solo se reescribe el año en curso. El precio ajustado es una
multiplicación al leer (``precio_ajustado()`` o la medida equivalente en Power BI).

Dividendos: gold/fact_dividendo (lo revisado y lo que tuvo consenso de los dos agentes), solo en
efectivo y aprobados; donde no hay, gold/fact_dividendo_web (historiales públicos verificados, ver
``dividendos_web.py``); los dividendos en acciones y los que solo están propuestos no ajustan la serie
y se cuentan en el resumen. Las escisiones y repartos de acciones de otra sociedad entran como
distribución en especie (columna ``distribucion``). Los splits y reagrupaciones de gold/fact_split (ver ``splits.py``) se
aplican el día que ocurren; los saltos de más de 50 % que queden sin explicar se marcan en
``salto_atipico``. Si un PDF no trae la fecha ex-dividendo, se estima con la regla de la
época (config/convenciones.yaml) y la fila queda marcada.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from invest import storage
from invest import fx
from invest import sucesiones
from invest.distribuciones import negociados, por_especie_y_fecha
from invest.calendario import dia_habil_siguiente_o_mismo, restar_dias_habiles
from invest.config import convenciones


def dias_periodo_ex(fecha_pago: date) -> int:
    reglas = sorted(convenciones()["dividendos"]["periodo_ex_dividendo"], key=lambda r: r["desde"])
    dias = reglas[0]["dias_habiles"]
    for r in reglas:
        if fecha_pago >= date.fromisoformat(str(r["desde"])):
            dias = r["dias_habiles"]
    return int(dias)


def estimar_fecha_ex(fecha_pago: date) -> date:
    """Primer día del periodo ex-dividendo según la regla de la época."""
    pago = dia_habil_siguiente_o_mismo(fecha_pago)
    return restar_dias_habiles(pago, dias_periodo_ex(pago))


def dividendos_por_especie(resumen: dict | None = None) -> pl.DataFrame:
    """Dividendos en efectivo (COP por acción) por especie y fecha ex-dividendo."""
    resumen = resumen if resumen is not None else {}
    vacio = pl.DataFrame(schema={"especie": pl.Utf8, "fecha_ex": pl.Date, "dividendo": pl.Float64,
                                 "fecha_ex_estimada": pl.Boolean, "revisado": pl.Boolean})
    div = storage.leer_tabla("gold", "fact_dividendo")
    activos = storage.leer_dim("dim_activo")
    if div is None or div.is_empty() or activos is None:
        web = _dividendos_web([], resumen)
        if not web:
            return vacio
        return pl.DataFrame(web).group_by(["especie", "fecha_ex"]).agg(
            pl.col("dividendo").sum(), pl.col("fecha_ex_estimada").any(), pl.col("revisado").all())
    resumen["en_acciones"] = div.filter(pl.col("forma") != "efectivo").height
    resumen["solo_propuestos"] = div.filter(pl.col("solo_propuesto")).height
    div = div.filter((pl.col("forma") == "efectivo") & ~pl.col("solo_propuesto"))

    filas, sin_fecha, otra_moneda = [], 0, 0
    tasas = _trm()
    clases = convenciones()["dividendos"]["clases_por_decreto"]
    for d in div.iter_rows(named=True):
        valor = d["valor_cuota"] if d["valor_cuota"] is not None else d["valor_por_accion"]
        if valor is None:
            continue
        fecha_ex, estimada = d["fecha_ex_dividendo"], False
        if fecha_ex is None:
            if d["fecha_pago"] is None:
                sin_fecha += 1
                continue
            fecha_ex, estimada = estimar_fecha_ex(d["fecha_pago"]), True
        moneda = (d["moneda"] or "COP").upper()
        if moneda != "COP":
            tasa = (tasas if moneda == "USD" else fx.tasas_a_cop(moneda)).get(fecha_ex)
            if tasa is None:
                otra_moneda += 1
                continue
            valor = valor * tasa
        for especie in activos.filter(
            (pl.col("emisor_id") == d["emisor_id"]) & pl.col("clase_accion").is_in(clases.get(d["clase_accion"], []))
        ).get_column("especie").to_list():
            filas.append({"especie": especie, "fecha_ex": fecha_ex, "dividendo": float(valor),
                          "fecha_ex_estimada": estimada, "revisado": bool(d["revisado"])})
    resumen["sin_fecha"] = sin_fecha
    resumen["moneda_sin_tasa"] = otra_moneda
    filas += _dividendos_web(filas, resumen)
    if not filas:
        return vacio
    # Ordinario y extraordinario con el mismo periodo ex-dividendo se suman
    return pl.DataFrame(filas).group_by(["especie", "fecha_ex"]).agg(
        pl.col("dividendo").sum(), pl.col("fecha_ex_estimada").any(), pl.col("revisado").all()
    )


def _dividendos_web(de_pdf: list[dict], resumen: dict) -> list[dict]:
    """Dividendos de gold/fact_dividendo_web para las acciones y fechas sin dividendo de los PDF.

    Los PDF de la SFC mandan: un pago web a 7 días o menos de uno de los PDF en la misma acción se
    descarta (es el mismo pago).
    """
    web = storage.leer_tabla("gold", "fact_dividendo_web")
    if web is None or web.is_empty():
        resumen["dividendos_web"] = 0
        return []
    pdf = {}
    for d in de_pdf:
        pdf.setdefault(d["especie"], []).append(d["fecha_ex"])
    filas = []
    tasas = None
    for r in web.iter_rows(named=True):
        if any(abs((r["fecha_ex"] - f).days) <= 7 for f in pdf.get(r["especie"], [])):
            continue
        valor = float(r["dividendo"])
        moneda = (r["moneda"] or "COP").upper()
        if moneda != "COP":  # Mineros (USD) con la TRM del día ex; Canacol (CAD) con TRM / CAD por USD
            tasas = {} if tasas is None else tasas
            if moneda not in tasas:
                tasas[moneda] = fx.tasas_a_cop(moneda)
            if r["fecha_ex"] not in tasas[moneda]:
                resumen["moneda_sin_tasa"] = resumen.get("moneda_sin_tasa", 0) + 1
                continue
            valor *= tasas[moneda][r["fecha_ex"]]
        filas.append({"especie": r["especie"], "fecha_ex": r["fecha_ex"], "dividendo": valor,
                      "fecha_ex_estimada": False, "revisado": bool(r["revisado"])})
    resumen["dividendos_web"] = len(filas)
    return filas


def _trm() -> dict[date, float]:
    fx = storage.leer_tabla("silver", "fact_fx_diaria")
    if fx is None:
        return {}
    usd = fx.filter((pl.col("moneda_origen") == "USD") & (pl.col("moneda_destino") == "COP"))
    return dict(usd.select("fecha", "tasa").iter_rows())


def construir() -> tuple[pl.DataFrame | None, dict]:
    """gold/fact_retorno_diario, por año y solo lo que cambia. Devuelve (tabla, resumen)."""
    resumen: dict = {}
    precios = sucesiones.precios_sfc()
    if precios is None or precios.is_empty():
        return None, resumen
    precios = precios.select("fecha", "especie", "precio_promedio", "valor_transado")
    resumen["dias_sin_negociacion_descartados"] = precios.height
    precios = negociados(precios)
    resumen["dias_sin_negociacion_descartados"] -= precios.height
    div = dividendos_por_especie(resumen)

    # Cada dividendo va al primer día negociado desde su fecha ex-dividendo
    if not div.is_empty():
        dias = precios.select("especie", "fecha").sort("fecha")
        div = div.sort("fecha_ex").join_asof(
            dias, left_on="fecha_ex", right_on="fecha", by="especie", strategy="forward",
            check_sortedness=False,  # ambas tablas van ordenadas por fecha, así que cada grupo también
        )
        resumen["dividendos_futuros"] = div.filter(pl.col("fecha").is_null()).height  # aún sin negociación
        div = div.filter(pl.col("fecha").is_not_null()).group_by(["especie", "fecha"]).agg(
            pl.col("dividendo").sum(), pl.col("fecha_ex").min(),
            pl.col("fecha_ex_estimada").any(), pl.col("revisado").all(),
        )
        resumen["dividendos_aplicados"] = div.height
        df = precios.join(div, on=["especie", "fecha"], how="left")
    else:
        resumen["dividendos_aplicados"] = 0
        df = precios.with_columns(
            pl.lit(None, pl.Float64).alias("dividendo"), pl.lit(None, pl.Date).alias("fecha_ex"),
            pl.lit(None, pl.Boolean).alias("fecha_ex_estimada"), pl.lit(None, pl.Boolean).alias("revisado"),
        )

    # Distribuciones en especie (escisiones): al primer día negociado desde su fecha ex
    dist = por_especie_y_fecha()
    if not dist.is_empty():
        dias = precios.select("especie", "fecha").sort("fecha")
        dist = dist.sort("fecha_ex").join_asof(
            dias, left_on="fecha_ex", right_on="fecha", by="especie", strategy="forward", check_sortedness=False,
        ).filter(pl.col("fecha").is_not_null()).group_by(["especie", "fecha"]).agg(pl.col("distribucion").sum())
        df = df.join(dist, on=["especie", "fecha"], how="left")
    else:
        df = df.with_columns(pl.lit(None, pl.Float64).alias("distribucion"))
    resumen["distribuciones_aplicadas"] = df.filter(pl.col("distribucion").is_not_null()).height

    # Splits aprobados o respaldados por un evento (gold/fact_split): el día del split el precio
    # anterior se expresa en acciones nuevas, para que el retorno sea el de mercado
    splits = storage.leer_tabla("gold", "fact_split")
    if splits is not None and not splits.is_empty():
        df = df.join(splits.select("especie", "fecha", pl.col("factor_acciones").alias("split_factor")),
                     on=["especie", "fecha"], how="left")
    else:
        df = df.with_columns(pl.lit(None, pl.Float64).alias("split_factor"))
    resumen["splits_aplicados"] = df.filter(pl.col("split_factor").is_not_null()).height

    anterior = pl.col("precio_promedio").shift(1).over("especie") / pl.col("split_factor").fill_null(1.0)
    df = df.with_columns(
        pl.col("dividendo").fill_null(0.0),
        pl.col("distribucion").fill_null(0.0),
        anterior.alias("_previo"),
        (pl.col("fecha") - pl.col("fecha").shift(1).over("especie")).dt.total_days().alias("dias_desde_anterior"),
    ).with_columns(
        # Un dividendo mayor que el precio anterior es un error de captura: no se aplica
        (pl.col("dividendo") >= pl.col("_previo")).fill_null(False).alias("dividendo_invalido"),
    ).with_columns(
        pl.when(pl.col("dividendo_invalido")).then(0.0).otherwise(pl.col("dividendo")).alias("dividendo"),
    ).with_columns(
        (pl.col("precio_promedio") / pl.col("_previo") - 1).alias("retorno_precio"),
        ((pl.col("precio_promedio") + pl.col("dividendo") + pl.col("distribucion")) / pl.col("_previo") - 1)
        .alias("retorno_total"),
        # Factor implícito en el índice: reinvertir lo recibido al precio del primer día ex
        (pl.col("precio_promedio") / (pl.col("precio_promedio") + pl.col("dividendo") + pl.col("distribucion")))
        .alias("factor_ajuste"),
    ).with_columns(
        (100 * (1 + pl.col("retorno_total").fill_null(0.0)).cum_prod().over("especie")).alias("indice_rt"),
    ).drop("_previo")
    resumen["dividendos_invalidos"] = df.filter(pl.col("dividendo_invalido")).height
    # Saltos que no son mercado: splits, cambios de valor nominal o errores de la fuente. La SFC no
    # ajusta sus precios por esos eventos; se marcan para que volatilidad, señales y backtests los
    # excluyan hasta que se ajusten.
    # Los revisados como "mercado" (OPA, quiebra, reanudación) son saltos reales: no se marcan.
    umbral = float(convenciones()["datos"].get("salto_atipico_accion", 0.5))
    reales = storage.leer_tabla("gold", "fact_salto_mercado")
    if reales is not None and not reales.is_empty():
        df = df.join(reales.select("especie", "fecha", pl.lit(True).alias("_mercado")),
                     on=["especie", "fecha"], how="left")
    else:
        df = df.with_columns(pl.lit(None, pl.Boolean).alias("_mercado"))
    df = df.with_columns(
        ((pl.col("retorno_precio").abs() > umbral) & pl.col("_mercado").is_null() & (pl.col("distribucion") == 0))
        .fill_null(False).alias("salto_atipico")).drop("_mercado")
    resumen["saltos_mercado"] = 0 if reales is None else reales.height
    resumen["saltos_atipicos"] = df.filter(pl.col("salto_atipico")).height

    df = df.select(
        "fecha", "especie", pl.col("especie").str.to_uppercase().alias("activo_id"), "precio_promedio",
        "valor_transado", "dias_desde_anterior", "dividendo", "fecha_ex", "fecha_ex_estimada", "revisado",
        "dividendo_invalido", "distribucion", "split_factor", "factor_ajuste", "retorno_precio", "retorno_total", "indice_rt",
        "salto_atipico",
    )
    storage.escribir_por_anio("gold", "fact_retorno_diario", df, "fecha")
    return df, resumen


def precio_ajustado(df: pl.DataFrame) -> pl.DataFrame:
    """Agrega precio_ajustado: el índice escalado para que el último día coincida con el precio real."""
    return df.sort(["especie", "fecha"]).with_columns(
        (pl.col("indice_rt") * pl.col("precio_promedio").last().over("especie")
         / pl.col("indice_rt").last().over("especie")).alias("precio_ajustado")
    )
