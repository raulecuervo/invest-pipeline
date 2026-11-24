"""Posiciones, asignación y desempeño de las carteras (a partir de usuario/fact_transaccion).

- ``gold/fact_posicion`` (cartera × especie × día hábil, más una fila ``EFECTIVO`` por cartera): cantidad,
  costo fiscal (promedio ponderado, en pesos, con comisiones), precio del día (último conocido de
  ``fact_precio_diario``), valor de mercado en pesos y en dólares, peso en la cartera y ganancia no
  realizada. Desde la primera transacción de cada cartera hasta el último precio disponible.
- ``gold/fact_asignacion`` (cartera × día × clase de activo): peso actual frente al objetivo del perfil
  vigente ese día y sus bandas; ``fuera_de_banda`` alimenta la alerta de rebalanceo.
- ``gold/fact_desempeno`` (cartera × mes): valor al inicio y al final, flujos externos (aportes menos
  retiros), retorno del mes por Dietz modificado (ajusta por la fecha de cada flujo), retorno acumulado
  ponderado por tiempo (TWR, para comparar con el benchmark), XIRR desde el inicio (el resultado propio
  con aportes y retiros), retorno del benchmark del perfil y exceso.

Efectivo: aportes y ventas suman; compras, retiros, comisiones e impuestos restan; los dividendos en
efectivo suman neto de retención. Todo se lleva en pesos (las transacciones en dólares con su TRM
aplicada). Los dividendos en acciones suman acciones con costo cero.

Los impuestos y costos son los que registra el usuario; las reglas tributarias de
usuario/dim_parametro_tributario se aplican solo cuando estén validadas por un contador.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from invest import storage, usuario
from invest.calendario import sesiones_entre

EFECTIVO = "EFECTIVO"


def _montos(t: pl.DataFrame) -> pl.DataFrame:
    """Monto bruto, costos y efecto en efectivo de cada transacción, en pesos."""
    a_cop = pl.when(pl.col("moneda") == "USD").then(pl.col("trm")).otherwise(1.0)
    bruto = pl.coalesce("monto", pl.col("cantidad") * pl.col("precio")).fill_null(0.0)
    costos = pl.col("comision").fill_null(0.0) + pl.col("gmf").fill_null(0.0)
    ret = pl.col("retencion").fill_null(0.0)
    efecto = (pl.when(pl.col("tipo") == "compra").then(-(bruto + costos))
              .when(pl.col("tipo") == "venta").then(bruto - costos - ret)
              .when(pl.col("tipo") == "dividendo").then(bruto - ret)
              .when(pl.col("tipo") == "aporte").then(bruto)
              .when(pl.col("tipo").is_in(["retiro", "comision", "impuesto"])).then(-bruto)
              .otherwise(0.0))
    acciones = (pl.when(pl.col("tipo").is_in(["compra", "dividendo_acciones"])).then(pl.col("cantidad"))
                .when(pl.col("tipo") == "venta").then(-pl.col("cantidad")).otherwise(0.0))
    externo = (pl.when(pl.col("tipo") == "aporte").then(bruto).when(pl.col("tipo") == "retiro").then(-bruto)
               .otherwise(0.0))
    return t.with_columns(
        (bruto * a_cop).alias("bruto_cop"), (costos * a_cop).alias("costos_cop"),
        (efecto * a_cop).alias("efectivo_cop"), acciones.fill_null(0.0).alias("delta_acciones"),
        (externo * a_cop).alias("flujo_externo_cop"),
        pl.coalesce("fecha_liquidacion", "fecha_operacion").alias("fecha_efectivo"),
    )


def _costo_promedio(t: pl.DataFrame) -> pl.DataFrame:
    """Costo fiscal acumulado por cartera y especie tras cada transacción (promedio ponderado)."""
    filas = []
    for (cartera, especie), g in t.filter(pl.col("especie").is_not_null() & (pl.col("delta_acciones") != 0)) \
            .sort(["fecha_operacion", "transaccion_id"]).group_by(["cartera_id", "especie"], maintain_order=True):
        cantidad, costo = 0.0, 0.0
        for r in g.iter_rows(named=True):
            if r["delta_acciones"] > 0:
                if r["tipo"] == "compra":
                    costo += r["bruto_cop"] + r["costos_cop"]
                cantidad += r["delta_acciones"]
            else:
                vendidas = min(-r["delta_acciones"], cantidad)
                costo -= costo * (vendidas / cantidad) if cantidad else 0.0
                cantidad -= vendidas
            filas.append({"cartera_id": cartera, "especie": especie, "fecha": r["fecha_operacion"],
                          "cantidad": cantidad, "costo_fiscal_cop": costo})
    if not filas:
        return pl.DataFrame(schema={"cartera_id": pl.Utf8, "especie": pl.Utf8, "fecha": pl.Date,
                                    "cantidad": pl.Float64, "costo_fiscal_cop": pl.Float64})
    return pl.DataFrame(filas).unique(subset=["cartera_id", "especie", "fecha"], keep="last", maintain_order=True)


def fact_posicion(transacciones: pl.DataFrame | None = None, hasta: date | None = None) -> pl.DataFrame | None:
    t = transacciones if transacciones is not None else usuario.leer("fact_transaccion")
    if t.is_empty():
        return None
    t = _montos(t)
    precios = storage.leer_tabla("gold", "fact_precio_diario")
    hasta = hasta or (precios.get_column("fecha").max() if precios is not None and not precios.is_empty() else date.today())
    from invest.mercado import _trm

    trm = _trm()
    partes = []
    for (cartera,), g in t.group_by(["cartera_id"]):
        inicio = g.get_column("fecha_operacion").min()
        dias = pl.DataFrame({"fecha": sesiones_entre(inicio, hasta)}).with_columns(pl.lit(cartera).alias("cartera_id"))
        if dias.is_empty():
            continue
        # Acciones: estado tras la última transacción hasta cada día
        costo = _costo_promedio(g)
        for (especie,), c in costo.group_by(["especie"]):
            serie = dias.with_columns(pl.lit(especie).alias("especie")).sort("fecha").join_asof(
                c.sort("fecha").select("fecha", "cantidad", "costo_fiscal_cop"), on="fecha", strategy="backward")
            partes.append(serie.filter(pl.col("cantidad").is_not_null()))
        # Efectivo acumulado (por fecha de liquidación)
        caja = (g.group_by("fecha_efectivo").agg(pl.col("efectivo_cop").sum()).sort("fecha_efectivo")
                .with_columns(pl.col("efectivo_cop").cum_sum().alias("cantidad")))
        serie = dias.sort("fecha").join_asof(caja.select(pl.col("fecha_efectivo").alias("fecha"), "cantidad"),
                                             on="fecha", strategy="backward")
        partes.append(serie.with_columns(pl.lit(EFECTIVO).alias("especie"), pl.col("cantidad").fill_null(0.0),
                                         pl.col("cantidad").fill_null(0.0).alias("costo_fiscal_cop")))
    if not partes:
        return None
    pos = pl.concat([p.select("fecha", "cartera_id", "especie", "cantidad", "costo_fiscal_cop") for p in partes])
    if precios is not None and not precios.is_empty():
        pr = precios.select("especie", pl.col("fecha").alias("_fp"), "precio").sort("_fp")
        pos = pos.sort("fecha").join_asof(pr, left_on="fecha", right_on="_fp", by="especie", strategy="backward",
                                          check_sortedness=False).drop("_fp")
    else:
        pos = pos.with_columns(pl.lit(None, pl.Float64).alias("precio"))
    pos = pos.sort("fecha").join_asof(trm, left_on="fecha", right_on="fecha_mercado", strategy="backward",
                                      check_sortedness=False)
    pos = pos.with_columns(
        pl.when(pl.col("especie") == EFECTIVO).then(1.0).otherwise(pl.col("precio")).alias("precio"),
    ).with_columns((pl.col("cantidad") * pl.col("precio")).alias("valor_cop")).with_columns(
        (pl.col("valor_cop") / pl.col("trm")).alias("valor_usd"),
        (pl.col("valor_cop") / pl.col("valor_cop").sum().over("cartera_id", "fecha")).alias("peso"),
        pl.when(pl.col("especie") == EFECTIVO).then(0.0)
        .otherwise(pl.col("valor_cop") - pl.col("costo_fiscal_cop")).alias("ganancia_no_realizada_cop"),
    )
    # Una posición cerrada (cantidad 0) deja de aparecer después del día en que se cerró
    pos = pos.filter((pl.col("especie") == EFECTIVO) | (pl.col("cantidad").abs() > 1e-9))
    return pos.select("fecha", "cartera_id", "especie", "cantidad", "costo_fiscal_cop", "precio", "valor_cop",
                      "valor_usd", "peso", "ganancia_no_realizada_cop").sort(["cartera_id", "fecha", "especie"])


def fact_asignacion(pos: pl.DataFrame) -> pl.DataFrame:
    carteras, perfiles, asignacion = usuario.leer("dim_cartera"), usuario.leer("dim_perfil"), usuario.leer("dim_perfil_asignacion")
    activos = storage.leer_dim("dim_activo")
    clase = (activos.select("especie", "clase_activo_id", "pais_id", "moneda_id") if activos is not None
             else pl.DataFrame(schema={"especie": pl.Utf8, "clase_activo_id": pl.Utf8, "pais_id": pl.Utf8, "moneda_id": pl.Utf8}))
    p = pos.join(clase, on="especie", how="left").with_columns(
        pl.when(pl.col("especie") == EFECTIVO).then(pl.lit("EFECTIVO")).otherwise(pl.col("clase_activo_id").fill_null("OTRO"))
        .alias("clase_activo_id"))
    actual = p.group_by("cartera_id", "fecha", "clase_activo_id").agg(pl.col("peso").sum(), pl.col("valor_cop").sum())
    # Perfil vigente de cada cartera en cada fecha (la versión con vigente_desde más reciente)
    vig = (actual.select("cartera_id", "fecha").unique()
           .join(carteras.select("cartera_id", "perfil_id"), on="cartera_id", how="left")
           .join(perfiles.select("perfil_id", "version", "vigente_desde", "vigente_hasta"), on="perfil_id", how="left")
           .filter(pl.col("vigente_desde").is_null() | ((pl.col("vigente_desde") <= pl.col("fecha"))
                   & (pl.col("vigente_hasta").is_null() | (pl.col("fecha") <= pl.col("vigente_hasta")))))
           .sort("vigente_desde").unique(subset=["cartera_id", "fecha"], keep="last"))
    obj = (vig.join(asignacion.group_by("perfil_id", "version", "clase_activo_id").agg(
        pl.col("peso_objetivo").sum(), pl.col("banda_minima").sum(), pl.col("banda_maxima").sum()),
        on=["perfil_id", "version"], how="left")
        .select("cartera_id", "fecha", "perfil_id", "version", "clase_activo_id", "peso_objetivo", "banda_minima",
                "banda_maxima"))
    t = actual.join(obj, on=["cartera_id", "fecha", "clase_activo_id"], how="full", coalesce=True)
    t = t.with_columns(pl.col("peso").fill_null(0.0), pl.col("valor_cop").fill_null(0.0)).with_columns(
        (pl.col("peso") - pl.col("peso_objetivo")).alias("desvio"),
        ((pl.col("peso") < pl.col("banda_minima")) | (pl.col("peso") > pl.col("banda_maxima"))).fill_null(False)
        .alias("fuera_de_banda"))
    return t.sort(["cartera_id", "fecha", "clase_activo_id"])


def xirr(flujos: list[tuple[date, float]]) -> float | None:
    """Tasa anual que hace cero el valor presente de los flujos (bisección; None si no hay cambio de signo)."""
    if len(flujos) < 2 or not any(v > 0 for _, v in flujos) or not any(v < 0 for _, v in flujos):
        return None
    t0 = min(d for d, _ in flujos)

    def vpn(r: float) -> float:
        return sum(v / (1 + r) ** ((d - t0).days / 365.25) for d, v in flujos)

    bajo, alto = -0.9999, 10.0
    if vpn(bajo) * vpn(alto) > 0:
        return None
    for _ in range(200):
        medio = (bajo + alto) / 2
        if vpn(bajo) * vpn(medio) <= 0:
            alto = medio
        else:
            bajo = medio
    return (bajo + alto) / 2


def fact_desempeno(pos: pl.DataFrame, transacciones: pl.DataFrame | None = None) -> pl.DataFrame:
    t = _montos(transacciones if transacciones is not None else usuario.leer("fact_transaccion"))
    indices = storage.leer_tabla("gold", "fact_indice_mercado")
    carteras = usuario.leer("dim_cartera")
    perfiles = usuario.leer("dim_perfil")
    valor = pos.group_by("cartera_id", "fecha").agg(pl.col("valor_cop").sum().alias("valor")).sort("fecha")
    filas = []
    for (cartera,), v in valor.group_by(["cartera_id"]):
        v = v.sort("fecha")
        flujos = t.filter((pl.col("cartera_id") == cartera) & (pl.col("flujo_externo_cop") != 0)) \
            .select(pl.col("fecha_efectivo").alias("fecha"), "flujo_externo_cop")
        fines = v.group_by(pl.col("fecha").dt.month_end().alias("_mes")).agg(pl.col("fecha").max()).sort("fecha")
        anterior_fecha, anterior_valor, acumulado = None, 0.0, 1.0
        flujos_xirr: list[tuple[date, float]] = []
        bench = None
        if indices is not None:
            perfil = carteras.filter(pl.col("cartera_id") == cartera).get_column("perfil_id").to_list()
            nombre = (perfiles.filter(pl.col("perfil_id").is_in(perfil)).sort("version").get_column("benchmark")
                      .drop_nulls().to_list() or ["universo_igual"])[-1]
            bench = indices.filter((pl.col("indice_id") == nombre) & (pl.col("moneda") == "COP")).select("fecha", "valor").sort("fecha")
        for fin in fines.get_column("fecha").to_list():
            valor_fin = v.filter(pl.col("fecha") == fin).get_column("valor")[0]
            if anterior_fecha is None:
                # Primer mes: desde el primer aporte (o el primer día con posiciones)
                del_mes = flujos.filter(pl.col("fecha") <= fin)
                inicio_periodo = del_mes.get_column("fecha").min() if del_mes.height else v.get_column("fecha").min()
            else:
                del_mes = flujos.filter((pl.col("fecha") > anterior_fecha) & (pl.col("fecha") <= fin))
                inicio_periodo = anterior_fecha
            dias = max((fin - inicio_periodo).days, 1)
            neto = float(del_mes.get_column("flujo_externo_cop").sum()) if del_mes.height else 0.0
            ponderado = sum(f * (fin - d).days / dias for d, f in del_mes.iter_rows())
            base = anterior_valor + ponderado
            retorno = (valor_fin - anterior_valor - neto) / base if base > 0 else None
            if retorno is not None:
                acumulado *= 1 + retorno
            flujos_xirr += [(d, -f) for d, f in del_mes.iter_rows()]
            tir = xirr(flujos_xirr + [(fin, valor_fin)])
            r_bench = None
            if bench is not None and not bench.is_empty():
                b = bench.filter(pl.col("fecha") <= fin)
                b0 = bench.filter(pl.col("fecha") <= inicio_periodo)
                if b.height and b0.height:
                    r_bench = b.get_column("valor")[-1] / b0.get_column("valor")[-1] - 1
            filas.append({"cartera_id": cartera, "mes": fin.replace(day=1), "fecha": fin, "valor_inicio_cop": anterior_valor,
                          "valor_fin_cop": valor_fin, "flujo_externo_cop": neto, "retorno_mes": retorno,
                          "twr_acumulado": acumulado - 1, "xirr_desde_inicio": tir, "retorno_benchmark": r_bench,
                          "exceso": (retorno - r_bench) if retorno is not None and r_bench is not None else None})
            anterior_fecha, anterior_valor = fin, valor_fin
    esquema = {"cartera_id": pl.Utf8, "mes": pl.Date, "fecha": pl.Date, "valor_inicio_cop": pl.Float64,
               "valor_fin_cop": pl.Float64, "flujo_externo_cop": pl.Float64, "retorno_mes": pl.Float64,
               "twr_acumulado": pl.Float64, "xirr_desde_inicio": pl.Float64, "retorno_benchmark": pl.Float64,
               "exceso": pl.Float64}
    return pl.DataFrame(filas, schema=esquema).sort(["cartera_id", "mes"])


def construir() -> dict:
    pos = fact_posicion()
    if pos is None or pos.is_empty():
        return {"carteras": 0}
    storage.escribir_completa("gold", "fact_posicion", pos)
    storage.escribir_completa("gold", "fact_asignacion", fact_asignacion(pos))
    des = fact_desempeno(pos)
    storage.escribir_completa("gold", "fact_desempeno", des)
    return {"carteras": pos.get_column("cartera_id").n_unique(), "posiciones_hoy":
            pos.filter((pl.col("fecha") == pos.get_column("fecha").max()) & (pl.col("especie") != EFECTIVO)).height}
