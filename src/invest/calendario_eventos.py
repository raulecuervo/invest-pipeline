"""Calendario de eventos (``gold/fact_evento_calendario``): lo que ya pasó y lo que viene.

Una fila por evento con ``fecha``, ``tipo``, emisor (y especie cuando aplica), descripción, valor y
dos marcas: ``estimada`` (la fecha sale de una regla, no de un documento) y ``futura`` (posterior al
día en que se construyó la tabla).

- ``dividendo_ex`` y ``dividendo_pago``: cuotas de gold/fact_dividendo (avisos de la SFC leídos por
  los dos agentes; si el aviso no trae fecha ex, se estima con la regla de la época) y, para los
  pagos que solo están en los historiales públicos, gold/fact_dividendo_web. Los dividendos solo
  propuestos (sin aprobación de la asamblea) quedan fuera.
- ``asamblea_convocatoria`` y ``asamblea_decision``: información relevante publicada en la SFC por
  emisores con acciones (la fecha es la de publicación, no la de la reunión).
- ``banrep_decision``: cambios de la tasa de política (serie banrep_59) y las próximas reuniones con
  decisión de config/calendario_macro.yaml.
- ``dane_ipc``: publicación del IPC (fecha real cuando el conector la tiene; la siguiente, estimada
  al quinto día hábil del mes).
"""

from __future__ import annotations

from datetime import date

import polars as pl

from invest import storage
from invest.calendario import sesiones_entre
from invest.config import cargar_yaml

ESQUEMA = {"fecha": pl.Date, "tipo": pl.Utf8, "emisor_id": pl.Utf8, "especie": pl.Utf8, "descripcion": pl.Utf8,
           "valor": pl.Float64, "moneda": pl.Utf8, "estimada": pl.Boolean, "fuente": pl.Utf8}


def _dividendos() -> list[pl.DataFrame]:
    from invest.retornos import estimar_fecha_ex

    partes = []
    div = storage.leer_tabla("gold", "fact_dividendo")
    if div is not None and not div.is_empty():
        d = div.filter(~pl.col("solo_propuesto").fill_null(False) & pl.col("fecha_pago").is_not_null())
        d = d.with_columns(
            pl.col("fecha_ex_dividendo").is_null().alias("_est"),
            pl.coalesce("fecha_ex_dividendo", pl.col("fecha_pago").map_elements(estimar_fecha_ex, return_dtype=pl.Date))
            .alias("_ex"),
            ("Dividendo " + pl.col("tipo").fill_null("") + " " + pl.col("forma").fill_null("") + ", ejercicio "
             + pl.col("ejercicio").cast(pl.Utf8) + ", cuota " + pl.col("cuota").cast(pl.Utf8)
             + pl.when(pl.col("clase_accion") != "todas").then(" (" + pl.col("clase_accion") + ")").otherwise(pl.lit("")))
            .alias("_desc"))
        for tipo, fecha, estimada in (("dividendo_ex", "_ex", pl.col("_est")), ("dividendo_pago", "fecha_pago", pl.lit(False))):
            partes.append(d.select(pl.col(fecha).alias("fecha"), pl.lit(tipo).alias("tipo"), "emisor_id",
                                   pl.lit(None, pl.Utf8).alias("especie"), pl.col("_desc").alias("descripcion"),
                                   pl.col("valor_cuota").alias("valor"), "moneda", estimada.alias("estimada"),
                                   pl.lit("fact_dividendo").alias("fuente")))
    web = storage.leer_tabla("gold", "fact_dividendo_web")
    activos = storage.leer_dim("dim_activo")
    if web is not None and not web.is_empty() and activos is not None:
        w = web.join(activos.select("especie", "emisor_id"), on="especie", how="left")
        for tipo, fecha in (("dividendo_ex", "fecha_ex"), ("dividendo_pago", "fecha_pago")):
            partes.append(w.filter(pl.col(fecha).is_not_null()).select(
                pl.col(fecha).alias("fecha"), pl.lit(tipo).alias("tipo"), "emisor_id", "especie",
                pl.lit("Dividendo en efectivo (historial público)").alias("descripcion"),
                pl.col("dividendo").alias("valor"), "moneda", pl.lit(False).alias("estimada"),
                pl.lit("fact_dividendo_web").alias("fuente")))
    return partes


def _asambleas() -> list[pl.DataFrame]:
    eventos = storage.leer_tabla("gold", "fact_evento_corporativo")
    activos = storage.leer_dim("dim_activo")
    if eventos is None or eventos.is_empty() or activos is None:
        return []
    emisores = activos.get_column("emisor_id").unique().to_list()
    e = eventos.filter(pl.col("categoria").is_in(["asamblea_convocatoria", "asamblea_decision"])
                       & pl.col("emisor_id").is_in(emisores))
    return [e.select("fecha", pl.col("categoria").alias("tipo"), "emisor_id", pl.lit(None, pl.Utf8).alias("especie"),
                     pl.col("resumen").str.slice(0, 300).alias("descripcion"), pl.lit(None, pl.Float64).alias("valor"),
                     pl.lit(None, pl.Utf8).alias("moneda"), pl.lit(False).alias("estimada"),
                     pl.lit("fact_evento_corporativo").alias("fuente"))]


def _macro(cfg: dict, hoy: date) -> list[pl.DataFrame]:
    partes = []
    macro = storage.leer_tabla("gold", "fact_macro")
    if macro is not None and not macro.is_empty():
        tpm = macro.filter(pl.col("serie_id") == "banrep_59").sort("fecha").with_columns(
            pl.col("valor").shift(1).alias("_ant"))
        cambios = tpm.filter(pl.col("_ant").is_not_null() & (pl.col("valor") != pl.col("_ant")))
        partes.append(cambios.select(
            "fecha", pl.lit("banrep_decision").alias("tipo"), pl.lit(None, pl.Utf8).alias("emisor_id"),
            pl.lit(None, pl.Utf8).alias("especie"),
            ("Tasa de política de " + pl.col("_ant").round(2).cast(pl.Utf8) + " % a "
             + pl.col("valor").round(2).cast(pl.Utf8) + " %").alias("descripcion"),
            "valor", pl.lit("pct").alias("moneda"), pl.lit(False).alias("estimada"), pl.lit("fact_macro").alias("fuente")))
        ipc = macro.filter(pl.col("serie_id") == "ipc_co").sort("fecha")
        if not ipc.is_empty():
            partes.append(ipc.select(
                pl.col("fecha_publicacion").alias("fecha"), pl.lit("dane_ipc").alias("tipo"),
                pl.lit(None, pl.Utf8).alias("emisor_id"), pl.lit(None, pl.Utf8).alias("especie"),
                ("IPC de " + pl.col("fecha").dt.strftime("%Y-%m")).alias("descripcion"), "valor",
                pl.lit("indice").alias("moneda"), pl.col("publicacion_estimada").alias("estimada"),
                pl.lit("fact_macro").alias("fuente")))
            # Próxima publicación: el mes siguiente al último dato, al n-ésimo día hábil del mes que sigue
            ultimo = ipc.get_column("fecha").max()
            mes = date(ultimo.year + (ultimo.month // 12), ultimo.month % 12 + 1, 1)
            siguiente = date(mes.year + (mes.month // 12), mes.month % 12 + 1, 1)
            habiles = sesiones_entre(siguiente, date(siguiente.year, siguiente.month, 28))
            n = int(cfg.get("dane_ipc_dia_habil", 5))
            if len(habiles) >= n:
                partes.append(pl.DataFrame([{
                    "fecha": habiles[n - 1], "tipo": "dane_ipc", "emisor_id": None, "especie": None,
                    "descripcion": f"IPC de {mes:%Y-%m} (fecha estimada)", "valor": None, "moneda": None,
                    "estimada": True, "fuente": "config/calendario_macro.yaml"}], schema=ESQUEMA))
    futuras = [r for r in cfg.get("banrep_decisiones", []) or [] if date.fromisoformat(str(r["fecha"])) >= hoy]
    if futuras:
        partes.append(pl.DataFrame([{
            "fecha": date.fromisoformat(str(r["fecha"])), "tipo": "banrep_decision", "emisor_id": None, "especie": None,
            "descripcion": "Reunión de la Junta Directiva con decisión de tasa", "valor": None, "moneda": None,
            "estimada": False, "fuente": r.get("fuente", "config/calendario_macro.yaml")} for r in futuras], schema=ESQUEMA))
    return partes


def fact_evento_calendario(hoy: date | None = None) -> pl.DataFrame:
    hoy = hoy or date.today()
    cfg = cargar_yaml("calendario_macro.yaml")
    partes = [p.select(list(ESQUEMA)).cast(ESQUEMA) for p in _dividendos() + _asambleas() + _macro(cfg, hoy)]
    if not partes:
        return pl.DataFrame(schema=ESQUEMA | {"futura": pl.Boolean})
    df = pl.concat(partes).filter(pl.col("fecha").is_not_null())
    # Un mismo dividendo puede estar en los avisos y en el historial público: se deja el de los avisos
    avisos = df.filter(pl.col("fuente") == "fact_dividendo")
    web = df.filter(pl.col("fuente") == "fact_dividendo_web").join(
        avisos.select("tipo", "emisor_id", "fecha").unique(), on=["tipo", "emisor_id", "fecha"], how="anti")
    resto = df.filter(~pl.col("fuente").is_in(["fact_dividendo", "fact_dividendo_web"]))
    return (pl.concat([avisos, web, resto])
            .with_columns((pl.col("fecha") > hoy).alias("futura")).sort(["fecha", "tipo", "emisor_id"]))


def construir() -> dict:
    df = fact_evento_calendario()
    if df.is_empty():
        return {}
    storage.escribir_por_anio("gold", "fact_evento_calendario", df, "fecha")
    return {"eventos": df.height, "futuros": df.filter(pl.col("futura")).height}
