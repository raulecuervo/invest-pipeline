"""Plantilla de bancos: métricas propias de los bancos con el CUIF mensual de la SFC.

Las métricas genéricas (ROE por acción de los anexos, apalancamiento = pasivos / activos) describen mal
a un banco: su pasivo son los depósitos, que son el negocio. Este módulo arma
``gold/fact_metrica_banco`` (emisor × corte × métrica, formato largo) con ``silver/fact_cuif_mensual``,
las cuentas de config/bancos.yaml y el mapeo de entidades del CUIF a emisores listados.

Reglas:

- **Agregados.** Cada agregado (cartera bruta, depósitos, deterioro...) es la suma de sus cuentas en
  config/bancos.yaml. Si no llegó ninguna de sus cuentas, el agregado y las métricas que lo usan quedan
  vacíos (sin error): así se comporta mientras el conector no haya bajado las ``cuentas_extra``.
- **Resultados de 12 meses.** Los saldos de las clases 4 y 5 y la 590000 son acumulados del año: se
  pasan a 12 meses con ``metricas.doce_meses`` (acumulado + diciembre anterior − mismo mes del año
  anterior).
- **Promedios.** Los denominadores de las métricas de 12 meses son el promedio de los 13 cierres
  mensuales (t−12 a t); si falta alguno, la métrica queda vacía.
- **Fecha disponible.** Corte + ``rezago_dias`` de la entrada ``cuif_datos_gov`` del catálogo (50 días:
  la SFC publica entre 45 y 50 días después del corte). Toda métrica usa cortes iguales o anteriores
  al suyo, así que esa es también la fecha de la cifra más tardía.
- **Holdings.** El CUIF es del banco (estados separados que reporta a la SFC), no del holding listado
  (Grupo Cibest, Davivienda Group, Grupo Aval): ``es_holding`` lo marca. Grupo Aval es la suma simple de
  sus cuatro bancos (``base = cuif_suma_bancos``).
- Entidades sin acciones listadas (GNB Sudameris) quedan con ``emisor_id = CUIF-<código>``: sirven de
  par de comparación.

Métricas (``METRICAS``): ROE, ROA y ROTE de 12 meses, margen neto de intereses (NIM), eficiencia, costo
del riesgo, cartera en riesgo (B a E), cartera C a E (aproximación a la cartera vencida, que el CUIF no
separa), cobertura, crecimiento de cartera y depósitos, cartera / depósitos, y los saldos de cartera
bruta, depósitos, patrimonio tangible y utilidad de 12 meses. Fórmulas en el README ("Plantilla de
bancos").
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from invest import storage
from invest.config import cargar_yaml
from invest.metricas import _cociente, _crecimiento, doce_meses

ESQUEMA = {"emisor_id": pl.Utf8, "nombre_entidad": pl.Utf8, "entidades": pl.Utf8, "fecha_corte": pl.Date,
           "fecha_disponible": pl.Date, "metrica": pl.Utf8, "valor": pl.Float64, "unidad": pl.Utf8,
           "es_holding": pl.Boolean, "base": pl.Utf8}

# metrica: (unidad, descripción corta)
METRICAS = {
    "roe_banco_12m": ("ratio", "utilidad 12m / patrimonio promedio"),
    "roa_banco_12m": ("ratio", "utilidad 12m / activo promedio"),
    "rote_12m": ("ratio", "utilidad 12m / patrimonio tangible promedio"),
    "nim_12m": ("ratio", "(intereses de cartera y mercado monetario + rendimiento de inversiones en deuda − intereses pagados) 12m / (cartera bruta + mercado monetario + inversiones) promedio"),
    "utilidad_filiales_12m": ("ratio", "utilidad por el método de participación (filiales) 12m / utilidad 12m"),
    "eficiencia_12m": ("ratio", "gastos de personal y administrativos 12m / margen financiero bruto 12m"),
    "costo_riesgo_12m": ("ratio", "deterioro neto de cartera 12m / cartera bruta promedio"),
    "indice_cartera_riesgo": ("ratio", "cartera calificada B a E / cartera bruta"),
    "indice_cartera_cde": ("ratio", "cartera calificada C a E / cartera bruta"),
    "cobertura_cartera_cde": ("ratio", "deterioro acumulado de cartera / cartera C a E"),
    "crecimiento_cartera_12m": ("ratio", "cartera bruta / la de 12 meses antes − 1"),
    "crecimiento_depositos_12m": ("ratio", "depósitos / los de 12 meses antes − 1"),
    "cartera_depositos": ("ratio", "cartera bruta / depósitos"),
    "cartera_bruta": ("pesos", "vivienda + consumo + comercial + microcrédito, con leasing"),
    "depositos": ("pesos", "depósitos y exigibilidades"),
    "patrimonio_tangible": ("pesos", "patrimonio − activos intangibles (incluida la plusvalía)"),
    "utilidad_12m": ("pesos", "utilidad de los últimos 12 meses"),
}

_SERIE = ["emisor_id", "fecha_corte", "fecha_disponible", "valor", "unidad"]


def config() -> dict:
    return cargar_yaml("bancos.yaml")


def rezago_dias() -> int:
    for f in cargar_yaml("catalogo_fuentes.yaml")["fuentes"]:
        if f["id"] == "cuif_datos_gov":
            return int(f.get("rezago_dias", 50))
    return 50


def cuentas_usadas(cfg: dict | None = None) -> set[str]:
    cfg = cfg or config()
    return {str(c) for a in cfg["agregados"].values() for c in a["cuentas"]}


def _normalizar(cuif: pl.DataFrame) -> pl.DataFrame:
    """Código de cuenta con 6 dígitos (``1408`` → ``140800``) y código de entidad sin espacios."""
    return cuif.with_columns(pl.col("cuenta").cast(pl.Utf8).str.strip_chars().str.pad_end(6, "0"),
                             pl.col("codigo_entidad").cast(pl.Utf8).str.strip_chars(),
                             pl.col("fecha_corte").dt.month_end())


def agregados_entidad(cuif: pl.DataFrame, cfg: dict | None = None) -> pl.DataFrame:
    """codigo_entidad × fecha_corte × agregado → valor (suma de sus cuentas)."""
    cfg = cfg or config()
    c = _normalizar(cuif).filter(pl.col("valor").is_not_null())
    # Meses en que la entidad reportó algo: una cuenta que no aparece ese mes no tiene
    # saldo (AV Villas no reporta el mercado monetario en 29 de 129 meses) y vale 0, siempre que la entidad
    # la haya reportado alguna vez (si nunca aparece, p. ej. una cuenta recién agregada, queda vacía)
    meses = c.select("codigo_entidad", "fecha_corte").unique()
    partes = []
    for nombre, d in cfg["agregados"].items():
        v = pl.col("valor").abs() if d.get("absoluto") else pl.col("valor")
        suma = (c.filter(pl.col("cuenta").is_in([str(x) for x in d["cuentas"]]))
                .group_by("codigo_entidad", "fecha_corte").agg(v.sum().alias("valor")))
        con_cuenta = suma.select("codigo_entidad").unique()
        completa = (meses.join(con_cuenta, on="codigo_entidad", how="semi")
                    .join(suma, on=["codigo_entidad", "fecha_corte"], how="full", coalesce=True)
                    .with_columns(pl.col("valor").fill_null(0.0)))
        partes.append(completa.with_columns(pl.lit(nombre).alias("agregado")))
    if not partes:
        return pl.DataFrame(schema={"codigo_entidad": pl.Utf8, "fecha_corte": pl.Date, "valor": pl.Float64,
                                    "agregado": pl.Utf8})
    return pl.concat(partes).select("codigo_entidad", "fecha_corte", "agregado", "valor")


def _clave(entidades: list[str]) -> str:
    return "+".join(str(e) for e in entidades)


def agregados_grupo(ag: pl.DataFrame, entidades: list[str]) -> pl.DataFrame:
    """Suma de las entidades del grupo, solo en los meses en que reportan todas."""
    n = len(entidades)
    return (ag.filter(pl.col("codigo_entidad").is_in([str(e) for e in entidades]))
            .group_by("fecha_corte", "agregado").agg(pl.col("valor").sum(), pl.len().alias("_n"))
            .filter(pl.col("_n") == n).drop("_n").with_columns(pl.lit(_clave(entidades)).alias("emisor_id")))


def _promedio_13(s: pl.DataFrame) -> pl.DataFrame:
    """Promedio de los 13 cierres mensuales t−12 a t (todos presentes)."""
    if s.is_empty():
        return s
    base = s.with_columns(pl.col("fecha_corte").dt.month_end())
    t = base.select(*_SERIE[:3], "unidad", pl.col("valor").alias("_v0"))
    for k in range(1, 13):
        t = t.join(base.select("emisor_id", pl.col("fecha_corte").dt.offset_by(f"{k}mo").dt.month_end()
                               .alias("fecha_corte"), pl.col("valor").alias(f"_v{k}")),
                   on=["emisor_id", "fecha_corte"], how="inner")
    return t.with_columns(pl.mean_horizontal(*[f"_v{k}" for k in range(13)]).alias("valor")).select(_SERIE)


def _operar(a: pl.DataFrame, b: pl.DataFrame, signo: float = -1.0) -> pl.DataFrame:
    """a + signo × b, en los cortes que tienen los dos."""
    return (a.join(b.select("emisor_id", "fecha_corte", pl.col("valor").alias("_b"),
                            pl.col("fecha_disponible").alias("_fd")), on=["emisor_id", "fecha_corte"])
            .with_columns((pl.col("valor") + signo * pl.col("_b")).alias("valor"),
                          pl.max_horizontal("fecha_disponible", "_fd").alias("fecha_disponible"))
            .select(_SERIE))


def metricas_grupo(ag: pl.DataFrame, rezago: int) -> dict[str, pl.DataFrame]:
    """Métricas de un grupo (una entidad o la suma de varias) a partir de sus agregados mensuales."""
    def s(nombre: str) -> pl.DataFrame:
        return (ag.filter(pl.col("agregado") == nombre)
                .with_columns((pl.col("fecha_corte") + timedelta(days=rezago)).alias("fecha_disponible"),
                              pl.lit("pesos").alias("unidad"))
                .select(_SERIE).sort(["emisor_id", "fecha_corte"]))

    utilidad12 = doce_meses(s("utilidad"))
    patrimonio, activo = s("patrimonio"), s("activo")
    tangible = _operar(patrimonio, s("intangibles"))
    cartera, depositos = s("cartera_bruta"), s("depositos")
    ni = doce_meses(_operar(s("ingresos_intereses"), s("gastos_intereses")))
    productivos = _operar(_operar(cartera, s("mercado_monetario"), signo=1.0), s("inversiones"), signo=1.0)
    admin = s("gastos_administrativos")
    # Margen financiero bruto = ingresos sin recuperaciones de deterioro − gastos que no son de personal,
    # administrativos ni de deterioro (= utilidad antes de impuestos + gastos administrativos + deterioro neto)
    mfb = _operar(_operar(s("ingresos_operacion"), s("recuperaciones_total")),
                  _operar(_operar(s("gastos_operacion"), admin), s("deterioro_gasto_total")))
    deterioro_neto = doce_meses(_operar(s("deterioro_gasto_cartera"), s("recuperaciones_cartera")))
    return {
        "roe_banco_12m": _cociente(utilidad12, _promedio_13(patrimonio)),
        "roa_banco_12m": _cociente(utilidad12, _promedio_13(activo)),
        "rote_12m": _cociente(utilidad12, _promedio_13(tangible)),
        "nim_12m": _cociente(ni, _promedio_13(productivos)),
        # Qué parte de la utilidad del banco viene de sus filiales: alta en GNB Sudameris y en los bancos
        # matrices; con ella el ROE del CUIF no mide solo el negocio bancario propio
        "utilidad_filiales_12m": _cociente(doce_meses(s("metodo_participacion")), utilidad12),
        "eficiencia_12m": _cociente(doce_meses(admin), doce_meses(mfb)),
        "costo_riesgo_12m": _cociente(deterioro_neto, _promedio_13(cartera)),
        "indice_cartera_riesgo": _cociente(s("cartera_b_e"), cartera),
        "indice_cartera_cde": _cociente(s("cartera_c_e"), cartera),
        "cobertura_cartera_cde": _cociente(s("deterioro_cartera"), s("cartera_c_e")),
        "crecimiento_cartera_12m": _crecimiento(cartera),
        "crecimiento_depositos_12m": _crecimiento(depositos),
        "cartera_depositos": _cociente(cartera, depositos),
        "cartera_bruta": cartera,
        "depositos": depositos,
        "patrimonio_tangible": tangible,
        "utilidad_12m": utilidad12,
    }


def _vacia() -> pl.DataFrame:
    return pl.DataFrame(schema=ESQUEMA)


def fact_metrica_banco(cuif: pl.DataFrame | None = None, cfg: dict | None = None,
                       rezago: int | None = None) -> pl.DataFrame:
    cfg = cfg or config()
    cuif = cuif if cuif is not None else storage.leer_tabla("silver", "fact_cuif_mensual")
    if cuif is None or cuif.is_empty():
        return _vacia()
    rezago = rezago_dias() if rezago is None else rezago
    ag = agregados_entidad(cuif, cfg)
    salida, calculados = [], {}
    for m in cfg["mapeo"]:
        entidades = [str(e) for e in m["entidades"]]
        clave = _clave(entidades)
        if clave not in calculados:
            g = agregados_grupo(ag, entidades)
            filas = [df.with_columns(pl.lit(nombre).alias("metrica"), pl.col("fecha_corte").dt.month_end())
                     for nombre, df in metricas_grupo(g, rezago).items() if not df.is_empty()]
            calculados[clave] = pl.concat([f.select(*_SERIE, "metrica") for f in filas]) if filas else None
        t = calculados[clave]
        if t is None:
            continue
        emisor = m.get("emisor_id") or f"CUIF-{clave}"
        desde = m.get("holding_desde")
        desde = date.fromisoformat(str(desde)) if desde else None
        holding = pl.lit(bool(m.get("holding")))
        if desde is not None:
            holding = holding & (pl.col("fecha_corte") >= desde)
        salida.append(t.with_columns(
            pl.lit(emisor).alias("emisor_id"), pl.lit(m.get("nombre") or emisor).alias("nombre_entidad"),
            pl.lit(",".join(entidades)).alias("entidades"), holding.alias("es_holding"),
            pl.lit("cuif_banco" if len(entidades) == 1 else "cuif_suma_bancos").alias("base"),
            pl.col("metrica").replace_strict({k: v[0] for k, v in METRICAS.items()}, default="ratio").alias("unidad")))
    if not salida:
        return _vacia()
    return (pl.concat([s.select(list(ESQUEMA)).cast(ESQUEMA) for s in salida])
            .filter(pl.col("valor").is_not_null() & pl.col("valor").is_finite())
            .sort(["emisor_id", "fecha_corte", "metrica"]))


def construir() -> dict:
    df = fact_metrica_banco()
    storage.escribir_completa("gold", "fact_metrica_banco", df)  # vacía si no hay CUIF: el contrato la encuentra
    if df.is_empty():
        return {"metricas": 0}
    ultimo = df.get_column("fecha_corte").max()
    hoy = df.filter(pl.col("fecha_corte") == ultimo)
    return {"metricas": df.height, "emisores": df.get_column("emisor_id").n_unique(), "ultimo_corte": ultimo,
            "metricas_ultimo_corte": sorted(hoy.get_column("metrica").unique().to_list())}
