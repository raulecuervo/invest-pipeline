"""Fundamentales por emisor y corte trimestral, con la fecha desde la que se conocían.

Une dos fuentes del SIMEV (``ingest/sfc_niif.py``) en ``gold/fact_fundamental``:

- **Anexos** (``silver/fact_anexo_sfc``), estados **separados** (individuales) de cada emisor:
  141 (sector real) y 187 (entidades públicas) "información económica general"; 211 (entidades
  vigiladas) acciones y accionistas. De ahí salen el valor patrimonial y la utilidad por acción, el
  dividendo por acción, los ingresos, los empleados y, en el 211, las acciones en circulación.
- **XBRL** (``silver/fact_xbrl_sfc``), estados **consolidados**: activos, pasivos, patrimonio
  (total y de la controladora), ingresos, utilidad de la controladora y utilidad por acción. Solo las
  cifras del periodo del informe (no los comparativos); las de resultados son acumuladas del año.

- **Supersociedades** (``silver/fact_ee_anual_supersociedades``, respaldo): activos, pasivos,
  patrimonio, ingresos operacionales y utilidad de los estados **separados** anuales (``base =
  separado_anual``, ``fuente = supersociedades``), con precisión de ± 5.000 millones y
  ``fecha_disponible`` = 30 de junio del año siguiente. Cubre a los emisores sin XBRL.

- **Estados publicados por la compañía** (``silver/fact_ee_compania``, ``ingest/estados_compania.py``):
  los mismos conceptos consolidados que el XBRL, leídos de los PDF de cada compañía con dos agentes
  (``base = consolidado``, ``fuente = pdf_compania``). Si para un emisor y corte hay XBRL, manda el
  XBRL y no entra ninguna cifra del PDF de ese corte (las métricas toman un valor por corte). Los
  emisores ``solo_cotejo`` de ``config/estados_compania.yaml`` (Ecopetrol) no entran: sirven para
  medir el lector. ``fecha_disponible`` = fecha de publicación detectada (``Last-Modified`` o la
  carpeta de carga); si no se conoce, corte + ``dias_disponible_pdf_intermedio`` (o
  ``dias_disponible_pdf_cierre`` en diciembre), o la fecha en que se descubrió el PDF si fue antes.

``cotejo()`` compara las fuentes entre sí (``gold/fact_cotejo_fundamental``): el patrimonio que dan
los anexos (valor patrimonial por acción × acciones en circulación) contra el de Supersociedades, y
lo mismo con la utilidad (utilidad por acción × acciones; aproximada si las acciones cambiaron en el
año, porque la utilidad por acción usa el promedio ponderado). ``cotejo_pdf_xbrl()`` compara las
cifras de los PDF de la compañía con las del XBRL donde existen las dos
(``gold/fact_cotejo_pdf_compania``): así se valida el lector con Ecopetrol, ISA u otros con XBRL.

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
        # Las otras filas TOTAL son porcentajes (100). Menos de 100.000 acciones no es un emisor listado:
        # es una celda mal diligenciada (El Cóndor dic-2025 reportó 1.367)
        if primera and primera >= MINIMO_ACCIONES:
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
    # Una utilidad por acción en cero exacto cuando otro anexo del mismo corte trae una cifra es un campo
    # sin diligenciar (Terpel dic-2025: 0 en el 211 y 709,07 en el 141)
    otra = (df.filter((pl.col("concepto") == "utilidad_accion") & (pl.col("valor") != 0))
            .select("emisor_id", "fecha_corte").unique().with_columns(pl.lit(True).alias("_otra")))
    df = (df.join(otra, on=["emisor_id", "fecha_corte"], how="left")
          .filter(~((pl.col("concepto") == "utilidad_accion") & (pl.col("valor") == 0) & pl.col("_otra").fill_null(False)))
          .drop("_otra"))
    return _a_dolares(_fecha_disponible_anexo(df)).select(list(ESQUEMA))


MINIMO_ACCIONES = 100_000


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
    # Los emisores que reportan en dólares: un hecho guardado como pesos se corrige aquí también (el conector
    # lo hace al leer, pero hay cortes guardados antes de marcar la moneda). Y los que transmiten el XBRL con
    # los montos sin escalar (xbrl_montos_en_unidades en config/emisores_rnve.yaml) no se multiplican por 1.000
    lista = emisores()
    en_usd = [e["emisor_id"] for e in lista if e.get("moneda_informes") == "USD"]
    en_unidades = [e["emisor_id"] for e in lista if e.get("xbrl_montos_en_unidades")]
    x = x.with_columns(
        pl.when(pl.col("emisor_id").is_in(en_usd) & (pl.col("escala") == "miles_de_pesos")).then(pl.lit("miles_de_dolares"))
        .when(pl.col("emisor_id").is_in(en_usd) & (pl.col("escala") == "pesos")).then(pl.lit("dolares"))
        .otherwise(pl.col("escala")).alias("escala"))
    factor = (pl.when(pl.col("emisor_id").is_in(en_unidades)).then(1.0)
              .when(pl.col("escala").is_in(["miles_de_pesos", "miles_de_dolares"])).then(1000.0).otherwise(1.0))
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


SUPERSOCIEDADES = {"activos": "activos", "pasivos": "pasivos", "patrimonio": "patrimonio_total",
                   "ingresos_operacionales": "ingresos_operacionales", "ganancia": "utilidad_total"}


def desde_supersociedades(ss: pl.DataFrame | None) -> pl.DataFrame:
    """Estados separados anuales de Supersociedades (respaldo; ver docstring del módulo)."""
    if ss is None or ss.is_empty():
        return pl.DataFrame(schema=ESQUEMA)
    largo = ss.filter(pl.col("emisor_id").is_not_null()).unpivot(
        index=["emisor_id", "fecha_corte"], on=list(SUPERSOCIEDADES), variable_name="concepto", value_name="valor")
    return (largo.filter(pl.col("valor").is_not_null())
            .with_columns(pl.col("concepto").replace_strict(SUPERSOCIEDADES),
                          pl.date(pl.col("fecha_corte").dt.year() + 1, 6, 30).alias("fecha_disponible"),
                          pl.lit("pesos").alias("unidad"), pl.lit("separado_anual").alias("base"),
                          pl.lit("supersociedades").alias("fuente"))
            .select(list(ESQUEMA)).cast(ESQUEMA))


# Tolerancias del cotejo: el patrimonio debe cuadrar casi exacto; la utilidad por acción usa el promedio
# de acciones del año, así que si hubo recompras, emisiones o escisiones se aleja más
TOLERANCIA_COTEJO = {"patrimonio": 0.02, "utilidad": 0.10}


def cotejo(fund: pl.DataFrame) -> pl.DataFrame:
    """Compara, por emisor y cierre anual, lo que dicen los anexos de la SFC y Supersociedades.

    ``estado``: ``ok`` dentro de la tolerancia (o de la precisión de Supersociedades), ``revisar`` si se
    aleja, ``sin_par`` si falta una de las dos cifras. Se comparan en pesos; ``diferencia`` es relativa
    a Supersociedades.
    """
    esquema = {"emisor_id": pl.Utf8, "fecha_corte": pl.Date, "concepto": pl.Utf8, "anexos": pl.Float64,
               "supersociedades": pl.Float64, "diferencia": pl.Float64, "tolerancia": pl.Float64, "estado": pl.Utf8,
               "nota": pl.Utf8}
    if fund is None or fund.is_empty():
        return pl.DataFrame(schema=esquema)
    dic = fund.filter(pl.col("fecha_corte").dt.month() == 12)

    def uno(concepto: str, base: str | None = None) -> pl.DataFrame:
        d = dic.filter(pl.col("concepto") == concepto)
        if base:
            d = d.filter(pl.col("base") == base)
        # Si dos anexos traen el mismo concepto, se usa el 141/187 (el informe de información económica)
        d = d.with_columns((pl.col("fuente") == "anexo_211").alias("_ultimo")).sort(["emisor_id", "fecha_corte", "_ultimo"])
        return d.unique(subset=["emisor_id", "fecha_corte"], keep="first", maintain_order=True).select(
            "emisor_id", "fecha_corte", pl.col("valor").alias(concepto))

    llave = ["emisor_id", "fecha_corte"]
    t = (uno("valor_patrimonial_accion", "separado").join(uno("acciones_circulacion"), on=llave, how="full", coalesce=True)
         .join(uno("utilidad_accion", "separado"), on=llave, how="full", coalesce=True)
         .join(uno("patrimonio_total", "separado_anual"), on=llave, how="full", coalesce=True)
         .join(uno("utilidad_total", "separado_anual"), on=llave, how="full", coalesce=True))
    from invest.ingest.supersociedades import PRECISION

    partes = []
    for concepto, por_accion, total in (("patrimonio", "valor_patrimonial_accion", "patrimonio_total"),
                                        ("utilidad", "utilidad_accion", "utilidad_total")):
        tol = TOLERANCIA_COTEJO[concepto]
        p = t.select(*llave, (pl.col(por_accion) * pl.col("acciones_circulacion")).alias("anexos"),
                     pl.col(total).alias("supersociedades"))
        p = p.filter(pl.col("anexos").is_not_null() | pl.col("supersociedades").is_not_null()).with_columns(
            pl.lit(concepto).alias("concepto"), pl.lit(tol).alias("tolerancia"),
            ((pl.col("anexos") - pl.col("supersociedades")) / pl.col("supersociedades").abs()).alias("diferencia"))
        dentro = ((pl.col("anexos") - pl.col("supersociedades")).abs()
                  <= pl.max_horizontal(tol * pl.col("supersociedades").abs(), pl.lit(PRECISION)))
        p = p.with_columns(
            pl.when(pl.col("anexos").is_null() | pl.col("supersociedades").is_null()).then(pl.lit("sin_par"))
            .when(dentro).then(pl.lit("ok")).otherwise(pl.lit("revisar")).alias("estado"),
            pl.when(pl.col("anexos").is_null()).then(pl.lit("faltan el valor por acción o las acciones en los anexos"))
            .when(pl.col("supersociedades").is_null()).then(pl.lit("el emisor no está en Supersociedades ese año"))
            .when(~dentro & (pl.lit(concepto) == "utilidad"))
            .then(pl.lit("la utilidad por acción usa el promedio de acciones del año: revisar si cambiaron"))
            .when(~dentro).then(pl.lit("el valor por acción y las acciones de los anexos no dan el patrimonio"))
            .otherwise(pl.lit(None)).alias("nota"))
        partes.append(p.select(list(esquema)))
    return pl.concat(partes).cast(esquema).sort(["emisor_id", "fecha_corte", "concepto"])


def desde_pdf_compania(ee: pl.DataFrame | None) -> pl.DataFrame:
    """Cifras con consenso de los estados publicados por la compañía (todas, también las de control)."""
    if ee is None or ee.is_empty():
        return pl.DataFrame(schema=ESQUEMA)
    cfg = convenciones().get("fundamentales", {})
    intermedio = int(cfg.get("dias_disponible_pdf_intermedio", 60))
    cierre = int(cfg.get("dias_disponible_pdf_cierre", 90))
    plazo = (pl.when(pl.col("fecha_corte").dt.month() == 12).then(pl.col("fecha_corte") + timedelta(days=cierre))
             .otherwise(pl.col("fecha_corte") + timedelta(days=intermedio)))
    descubierto = pl.col("fecha_descubierto") if "fecha_descubierto" in ee.columns else pl.lit(None, pl.Date)
    detectada = pl.when(pl.col("fecha_publicacion") >= pl.col("fecha_corte")).then(pl.col("fecha_publicacion"))
    # Sin fecha detectada: el plazo conservador, salvo que el PDF ya se tuviera antes (se publicó a más tardar ese día)
    respaldo = pl.when(descubierto.is_not_null() & (descubierto < plazo) & (descubierto >= pl.col("fecha_corte"))) \
        .then(descubierto).otherwise(plazo)
    return (ee.filter((pl.col("estado") == "consenso") & pl.col("valor").is_not_null()
                      & pl.col("concepto").is_in(list(XBRL.values())))
            .with_columns(pl.coalesce(detectada, respaldo).alias("fecha_disponible"),
                          pl.lit("consolidado").alias("base"), pl.lit("pdf_compania").alias("fuente"))
            .unique(subset=["emisor_id", "fecha_corte", "concepto"], keep="last")
            .select(list(ESQUEMA)).cast(ESQUEMA))


def con_prioridad_xbrl(pdf: pl.DataFrame, xbrl: pl.DataFrame) -> pl.DataFrame:
    """Lo del PDF que entra a fact_fundamental: solo los emisor × corte sin XBRL y sin los emisores de
    control (``solo_cotejo``)."""
    if pdf.is_empty():
        return pdf
    from invest.ingest.estados_compania import solo_cotejo

    con_xbrl = xbrl.select("emisor_id", "fecha_corte").unique()
    return (pdf.filter(~pl.col("emisor_id").is_in(list(solo_cotejo())))
            .join(con_xbrl, on=["emisor_id", "fecha_corte"], how="anti"))


# Montos: los PDF suelen venir en millones o miles de millones y el XBRL en miles, así que el redondeo
# aleja hasta 0,5 de la unidad del PDF; 0,2 % cubre eso con margen. Por acción: 0,05 o 0,2 %
TOLERANCIA_COTEJO_PDF = {"relativa": 0.002, "por_accion": 0.05}
ESQUEMA_COTEJO_PDF = {"emisor_id": pl.Utf8, "fecha_corte": pl.Date, "concepto": pl.Utf8, "pdf_compania": pl.Float64,
                      "xbrl": pl.Float64, "diferencia": pl.Float64, "unidad": pl.Utf8, "estado": pl.Utf8}


def cotejo_pdf_xbrl(pdf: pl.DataFrame, xbrl: pl.DataFrame) -> pl.DataFrame:
    """PDF de la compañía contra XBRL, por emisor × corte con las dos fuentes. ``estado``: ``ok``,
    ``revisar`` (fuera de la tolerancia) o ``sin_par`` (el concepto está en una sola)."""
    if pdf.is_empty() or xbrl.is_empty():
        return pl.DataFrame(schema=ESQUEMA_COTEJO_PDF)
    llave = ["emisor_id", "fecha_corte"]
    ambos = pdf.select(llave).unique().join(xbrl.select(llave).unique(), on=llave)
    p = pdf.join(ambos, on=llave).select(*llave, "concepto", pl.col("valor").alias("pdf_compania"),
                                          pl.col("unidad").alias("_up"))
    x = xbrl.join(ambos, on=llave).select(*llave, "concepto", pl.col("valor").alias("xbrl"), pl.col("unidad").alias("_ux"))
    t = p.join(x, on=[*llave, "concepto"], how="full", coalesce=True)
    por_accion = pl.coalesce("_up", "_ux").str.ends_with("_por_accion")
    tol = pl.when(por_accion).then(pl.max_horizontal(pl.lit(TOLERANCIA_COTEJO_PDF["por_accion"]),
                                                     TOLERANCIA_COTEJO_PDF["relativa"] * pl.col("xbrl").abs())) \
        .otherwise(TOLERANCIA_COTEJO_PDF["relativa"] * pl.col("xbrl").abs())
    return (t.with_columns(
        ((pl.col("pdf_compania") - pl.col("xbrl")) / pl.col("xbrl").abs()).alias("diferencia"),
        pl.coalesce("_up", "_ux").alias("unidad"),
        pl.when(pl.col("pdf_compania").is_null() | pl.col("xbrl").is_null()).then(pl.lit("sin_par"))
        .when((pl.col("pdf_compania") - pl.col("xbrl")).abs() <= tol).then(pl.lit("ok"))
        .otherwise(pl.lit("revisar")).alias("estado"))
        .select(list(ESQUEMA_COTEJO_PDF)).cast(ESQUEMA_COTEJO_PDF).sort([*llave, "concepto"]))


def construir() -> tuple[pl.DataFrame | None, dict]:
    anexos = storage.leer_tabla("silver", "fact_anexo_sfc")
    xbrl = storage.leer_tabla("silver", "fact_xbrl_sfc")
    ss = storage.leer_tabla("silver", "fact_ee_anual_supersociedades")
    ee = storage.leer_tabla("silver", "fact_ee_compania")
    del_xbrl = desde_xbrl(xbrl)
    del_pdf = desde_pdf_compania(ee)
    df = pl.concat([desde_anexos(anexos), del_xbrl, desde_supersociedades(ss),
                    con_prioridad_xbrl(del_pdf, del_xbrl)]).sort(["emisor_id", "fecha_corte", "base", "concepto"])
    if df.is_empty():
        return None, {}
    storage.escribir_completa("gold", "fact_fundamental", df)
    c = cotejo(df)
    if not c.is_empty():
        storage.escribir_completa("gold", "fact_cotejo_fundamental", c)
    # Se escribe aunque esté vacía: el contrato del modelo espera la tabla
    c_pdf = cotejo_pdf_xbrl(del_pdf, del_xbrl)
    storage.escribir_completa("gold", "fact_cotejo_pdf_compania", c_pdf)
    return df, {
        "cotejo_ok": c.filter(pl.col("estado") == "ok").height,
        "cotejo_revisar": c.filter(pl.col("estado") == "revisar").height,
        "con_respaldo_anual": df.filter(pl.col("base") == "separado_anual").get_column("emisor_id").n_unique(),
        "emisores": df.get_column("emisor_id").n_unique(),
        "cortes": df.get_column("fecha_corte").n_unique(),
        "con_valor_patrimonial": df.filter(pl.col("concepto") == "valor_patrimonial_accion")
        .get_column("emisor_id").n_unique(),
        "con_consolidado": df.filter(pl.col("base") == "consolidado").get_column("emisor_id").n_unique(),
        "con_pdf_compania": df.filter(pl.col("fuente") == "pdf_compania").get_column("emisor_id").n_unique(),
        "cotejo_pdf_ok": c_pdf.filter(pl.col("estado") == "ok").height,
        "cotejo_pdf_revisar": c_pdf.filter(pl.col("estado") == "revisar").height,
        "filas": df.height,
    }
