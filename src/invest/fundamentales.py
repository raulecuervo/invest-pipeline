"""Fundamentales por emisor y corte trimestral, con la fecha desde la que se conocían.

Une dos fuentes del SIMEV (``ingest/sfc_niif.py``) en ``gold/fact_fundamental``:

- **Anexos** (``silver/fact_anexo_sfc``), estados **separados** (individuales) de cada emisor:
  141 (sector real) y 187 (entidades públicas) "información económica general"; 211 (entidades
  vigiladas) acciones y accionistas. De ahí salen el valor patrimonial y la utilidad por acción, el
  dividendo por acción, los ingresos, los empleados y, en el 211, las acciones en circulación.
- **XBRL** (``silver/fact_xbrl_sfc``), estados **consolidados**: activos, pasivos, patrimonio
  (total y de la controladora), ingresos, utilidad de la controladora y utilidad por acción. Solo las
  cifras del periodo del informe (no los comparativos); las de resultados son acumuladas del año.

Unidades: todo en pesos (el XBRL viene en miles y se multiplica por 1.000), salvo Canacol, que
reporta en dólares (``unidad`` = ``usd``). Las cifras por acción quedan en pesos por acción.

``fecha_disponible`` evita el sesgo de anticipación: para el XBRL es la fecha de transmisión; para
los anexos, que no la traen, se usa una fecha conservadora (corte + ``dias_disponible_intermedio``
o + ``dias_disponible_cierre`` en diciembre, de ``config/convenciones.yaml`` sección
``fundamentales``). Un factor a fin de mes solo puede usar filas con ``fecha_disponible`` anterior.
"""

from __future__ import annotations

import unicodedata
from datetime import timedelta

import polars as pl

from invest import storage
from invest.config import convenciones
from invest.ingest.sfc_niif import emisores

ESQUEMA = {
    "emisor_id": pl.Utf8, "fecha_corte": pl.Date, "fecha_disponible": pl.Date, "concepto": pl.Utf8,
    "valor": pl.Float64, "unidad": pl.Utf8, "base": pl.Utf8, "fuente": pl.Utf8,
}

# concepto del anexo normalizado (mayúsculas, sin tildes ni asteriscos) → (concepto, unidad)
ANEXOS = {
    "VALOR PATRIMONIAL X ACCION SIN VALORIZACION": ("valor_patrimonial_accion", "pesos_por_accion"),
    "VALOR PATRIMONIAL POR ACCION SIN VALORIZACION": ("valor_patrimonial_accion", "pesos_por_accion"),
    "VALOR PATRIMONIAL POR ACCION": ("valor_patrimonial_accion", "pesos_por_accion"),
    "UTILIDAD POR ACCION": ("utilidad_accion", "pesos_por_accion"),
    "PERDIDA POR ACCION": ("perdida_accion", "pesos_por_accion"),
    "VALOR DIVIDENDO POR ACCION ORDINARIA EN EFECTIVO": ("dividendo_accion_ordinaria", "pesos_por_accion"),
    "TOTAL INGRESOS VTAS Y/O PRESTACION SERVICIOS": ("ingresos_separados", "pesos"),
    "TOTAL INGRESOS POR VENTAS Y/O PRESTACION DESERVICI": ("ingresos_separados", "pesos"),
    "NUMERO TOTAL DE EMPLEADOS": ("empleados", "personas"),
    "COTIZACION DE LA ACCION AL CIERRE DEL PERIODO": ("precio_cierre_reportado", "pesos_por_accion"),
    "VALOR NOMINAL DE LA ACCION": ("valor_nominal", "pesos_por_accion"),
}
XBRL = {
    "ifrs:Assets": "activos",
    "ifrs:Liabilities": "pasivos",
    "ifrs:Equity": "patrimonio_total",
    "ifrs:EquityAttributableToOwnersOfParent": "patrimonio_controladora",
    "ifrs:CashAndCashEquivalents": "efectivo",
    "ifrs:Revenue": "ingresos_consolidados",
    "ifrs:ProfitLoss": "utilidad_total",
    "ifrs:ProfitLossAttributableToOwnersOfParent": "utilidad_controladora",
    "ifrs:BasicEarningsLossPerShare": "upa_basica",
}


def normalizar(texto: str | None) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(t.replace("*", " ").upper().split())


def _fecha_disponible_anexo(df: pl.DataFrame) -> pl.DataFrame:
    cfg = convenciones().get("fundamentales", {})
    intermedio = int(cfg.get("dias_disponible_intermedio", 60))
    cierre = int(cfg.get("dias_disponible_cierre", 100))
    return df.with_columns(
        pl.when(pl.col("fecha_corte").dt.month() == 12)
        .then(pl.col("fecha_corte") + timedelta(days=cierre))
        .otherwise(pl.col("fecha_corte") + timedelta(days=intermedio)).alias("fecha_disponible"))


def desde_anexos(anexos: pl.DataFrame) -> pl.DataFrame:
    if anexos is None or anexos.is_empty():
        return pl.DataFrame(schema=ESQUEMA)
    a = anexos.filter(pl.col("formato").is_in(["141", "187", "211"]) & ~pl.col("consolidado")).with_columns(
        pl.col("concepto").map_elements(normalizar, return_dtype=pl.Utf8).alias("_c"))
    filas = []
    # 141 y 187: una columna de valor. 211: el valor por acción viene en cualquiera de las dos columnas
    for (emisor, corte, formato, c), g in a.group_by(["emisor_id", "fecha_corte", "formato", "_c"]):
        if c not in ANEXOS:
            continue
        concepto, unidad = ANEXOS[c]
        valores = [v for v in g.get_column("valor").to_list() if v is not None]
        if not valores:
            continue
        valor = max(valores, key=abs) if formato == "211" else valores[0]
        filas.append({"emisor_id": emisor, "fecha_corte": corte, "concepto": concepto, "valor": float(valor),
                      "unidad": unidad, "base": "separado", "fuente": f"anexo_{formato}"})
    # Acciones en circulación (211): la primera fila TOTAL de la tabla por tipo de acción
    t211 = a.filter((pl.col("formato") == "211") & (pl.col("_c") == "TOTAL")
                    & (pl.col("columna").map_elements(normalizar, return_dtype=pl.Utf8) == "NUMERO DE ACCIONES"))
    for (emisor, corte), g in t211.group_by(["emisor_id", "fecha_corte"]):
        primera = g.sort("fila").get_column("valor").to_list()[0]
        if primera and primera > 100:  # las otras filas TOTAL son porcentajes (100)
            filas.append({"emisor_id": emisor, "fecha_corte": corte, "concepto": "acciones_circulacion",
                          "valor": float(primera), "unidad": "acciones", "base": "separado", "fuente": "anexo_211"})
    filas += _acciones_por_clase(a)
    df = pl.DataFrame(filas, schema={k: v for k, v in ESQUEMA.items() if k != "fecha_disponible"})
    if df.is_empty():
        return pl.DataFrame(schema=ESQUEMA)
    # Utilidad neta por acción = utilidad - pérdida (los anexos las reportan por separado)
    perdida = df.filter(pl.col("concepto") == "perdida_accion").select("emisor_id", "fecha_corte", "fuente",
                                                                        pl.col("valor").alias("_p"))
    df = (df.filter(pl.col("concepto") != "perdida_accion")
          .join(perdida, on=["emisor_id", "fecha_corte", "fuente"], how="left")
          .with_columns(pl.when(pl.col("concepto") == "utilidad_accion")
                        .then(pl.col("valor") - pl.col("_p").fill_null(0.0)).otherwise(pl.col("valor")).alias("valor"))
          .drop("_p"))
    # Un valor patrimonial en cero o negativo es un campo sin diligenciar (p. ej. Promigas, sep-2025)
    df = df.filter(~((pl.col("concepto") == "valor_patrimonial_accion") & (pl.col("valor") <= 0)))
    return _a_dolares(_fecha_disponible_anexo(df)).select(list(ESQUEMA))


CLASES_211 = {
    "ACCIONES ORDINARIAS": "acciones_ordinarias",
    "ACCIONES CON DIVIDENDO PREFERENCIAL Y SIN DERECHO A VOTO": "acciones_preferenciales",
    "ACCIONES PREFERENCIALES": "acciones_preferenciales",
    "ACCIONES PRIVILEGIADAS": "acciones_privilegiadas",
}


def _acciones_por_clase(a: pl.DataFrame) -> list[dict]:
    """Acciones en circulación por clase (211): las filas de la primera tabla, antes de su TOTAL.

    Más abajo el formato repite nombres de clase en otras tablas (p. ej. acciones readquiridas), por
    eso solo cuentan las filas anteriores al primer TOTAL con número de acciones.
    """
    t = a.filter((pl.col("formato") == "211")
                 & (pl.col("columna").map_elements(normalizar, return_dtype=pl.Utf8) == "NUMERO DE ACCIONES"))
    filas = []
    for (emisor, corte, _), g in t.group_by(["emisor_id", "fecha_corte", "grupo"]):
        g = g.sort("fila")
        totales = g.filter((pl.col("_c") == "TOTAL") & (pl.col("valor") > 100)).get_column("fila")
        if totales.is_empty():
            continue
        vistas: dict[str, float] = {}
        for r in g.filter(pl.col("fila") < totales.min()).iter_rows(named=True):
            concepto = CLASES_211.get(r["_c"])
            if concepto and concepto not in vistas and r["valor"] is not None:
                vistas[concepto] = float(r["valor"])
        filas += [{"emisor_id": emisor, "fecha_corte": corte, "concepto": k, "valor": v, "unidad": "acciones",
                   "base": "separado", "fuente": "anexo_211"} for k, v in vistas.items() if v > 0]
    # El 211 trimestral y el semestral de un mismo corte traen lo mismo: una fila por concepto
    unicas = {(f["emisor_id"], f["fecha_corte"], f["concepto"]): f for f in filas}
    return list(unicas.values())


def _a_dolares(df: pl.DataFrame) -> pl.DataFrame:
    """Los emisores que reportan en dólares (config/emisores_rnve.yaml, moneda_informes) quedan en usd."""
    en_usd = [e["emisor_id"] for e in emisores() if e.get("moneda_informes") == "USD"]
    return df.with_columns(
        pl.when(pl.col("emisor_id").is_in(en_usd)).then(pl.col("unidad").replace({"pesos": "usd", "pesos_por_accion": "usd_por_accion"}))
        .otherwise(pl.col("unidad")).alias("unidad"))


def desde_xbrl(xbrl: pl.DataFrame) -> pl.DataFrame:
    if xbrl is None or xbrl.is_empty():
        return pl.DataFrame(schema=ESQUEMA)
    x = xbrl.filter(
        pl.col("concepto").is_in(list(XBRL)) & (pl.col("fecha_fin") == pl.col("fecha_corte"))
        & ((pl.col("tipo_periodo") == "instante")
           | (pl.col("fecha_inicio") == pl.date(pl.col("fecha_corte").dt.year(), 1, 1))))
    factor = (pl.when(pl.col("escala").is_in(["miles_de_pesos", "miles_de_dolares"])).then(1000.0).otherwise(1.0))
    unidad = (pl.when(pl.col("escala") == "dolares").then(pl.lit("usd_por_accion"))
              .when(pl.col("escala") == "miles_de_dolares").then(pl.lit("usd"))
              .when(pl.col("escala") == "pesos").then(pl.lit("pesos_por_accion"))
              .otherwise(pl.lit("pesos")))
    return (x.with_columns(
        pl.col("concepto").replace_strict(XBRL).alias("concepto"),
        (pl.col("valor") * factor).alias("valor"), unidad.alias("unidad"),
        pl.lit("consolidado").alias("base"), pl.lit("xbrl").alias("fuente"),
        pl.coalesce("fecha_transmision", pl.col("fecha_corte") + timedelta(days=60)).alias("fecha_disponible"))
        .unique(subset=["emisor_id", "fecha_corte", "concepto"], keep="last")
        .select(list(ESQUEMA)))


def construir() -> tuple[pl.DataFrame | None, dict]:
    anexos = storage.leer_tabla("silver", "fact_anexo_sfc")
    xbrl = storage.leer_tabla("silver", "fact_xbrl_sfc")
    df = pl.concat([desde_anexos(anexos), desde_xbrl(xbrl)]).sort(["emisor_id", "fecha_corte", "base", "concepto"])
    if df.is_empty():
        return None, {}
    storage.escribir_completa("gold", "fact_fundamental", df)
    return df, {
        "emisores": df.get_column("emisor_id").n_unique(),
        "cortes": df.get_column("fecha_corte").n_unique(),
        "con_valor_patrimonial": df.filter(pl.col("concepto") == "valor_patrimonial_accion")
        .get_column("emisor_id").n_unique(),
        "con_consolidado": df.filter(pl.col("base") == "consolidado").get_column("emisor_id").n_unique(),
        "filas": df.height,
    }
