"""Sucesiones de especies (``config/sucesiones.yaml``): la historia de la antecesora se pega al
comienzo de la serie de la sucesora.

Todos los cálculos que leen los precios diarios de la SFC (retornos, liquidez, splits, dividendos
web, escisiones, dimensiones) usan :func:`precios_sfc` en lugar de leer ``silver/fact_precio_sfc``
directamente. Las filas de la antecesora se renombran con el código de la sucesora (precio dividido
por ``acciones_por_accion``) solo hasta ``ultimo_dia`` y antes de la primera rueda de la sucesora.

De dónde sale la historia de la antecesora, en este orden:

1. ``silver/fact_precio_sfc`` con el código de la antecesora (si se conoce ``especie_sfc``, el
   conector de precios lo baja una vez aunque la SFC ya no lo liste).
2. ``invest-data/fuentes_web/precios/<respaldo_csv>`` (fecha, precio, valor_transado opcional),
   cargado a mano o desde un historial público; ``fuente_precio`` lo dice.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import yaml

from invest import storage
from invest.config import CONFIG_DIR

CARPETA_CSV = ("fuentes_web", "precios")
COLUMNAS = ("fecha", "especie", "codigo_titulo", "precio_promedio", "valor_transado", "fuente_id", "ingest_ts")


def cargar() -> list[dict]:
    ruta = CONFIG_DIR / "sucesiones.yaml"
    if not ruta.exists():
        return []
    return list((yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}).get("sucesiones") or [])


def especies_antecesoras() -> list[str]:
    """Códigos SFC de antecesoras conocidas (el conector de precios los consulta aunque no estén en el índice)."""
    return [s["antecesora"]["especie_sfc"] for s in cargar() if (s.get("antecesora") or {}).get("especie_sfc")]


def _csv(nombre: str | None) -> pl.DataFrame | None:
    if not nombre:
        return None
    ruta = storage.data_dir().joinpath(*CARPETA_CSV, nombre)
    if not ruta.exists():
        return None
    df = pl.read_csv(ruta, try_parse_dates=True)
    if "valor_transado" not in df.columns:
        df = df.with_columns(pl.lit(None, pl.Float64).alias("valor_transado"))
    return df.select(pl.col("fecha").cast(pl.Date), pl.col("precio").cast(pl.Float64).alias("precio_promedio"),
                     pl.col("valor_transado").cast(pl.Float64))


def aplicar(precios: pl.DataFrame, lista: list[dict] | None = None,
            respaldos: dict[str, pl.DataFrame] | None = None) -> pl.DataFrame:
    """``precios`` (formato de silver/fact_precio_sfc) con la historia de cada antecesora pegada a su sucesora.

    ``respaldos`` (nombre de CSV → tabla) es para las pruebas; si falta, se leen los CSV de invest-data."""
    lista = cargar() if lista is None else lista
    if not lista or precios is None:
        return precios
    partes = [precios]
    for s in lista:
        suc, ant = s["especie"], s.get("antecesora") or {}
        rel = float(s.get("acciones_por_accion") or 1)
        ultimo = s["ultimo_dia"] if isinstance(s["ultimo_dia"], date) else date.fromisoformat(str(s["ultimo_dia"]))
        propias = precios.filter(pl.col("especie") == suc)
        inicio = propias.get_column("fecha").min() if not propias.is_empty() else None
        tope = min(ultimo, inicio) if inicio else ultimo
        hist = None
        if ant.get("especie_sfc"):
            h = precios.filter((pl.col("especie") == ant["especie_sfc"]) & (pl.col("fecha") <= tope))
            if inicio:
                h = h.filter(pl.col("fecha") < inicio)
            if not h.is_empty():
                hist = h.with_columns(pl.lit(suc).alias("especie"), (pl.col("precio_promedio") / rel).alias("precio_promedio"),
                                      pl.lit(f"sucesion:{ant['especie_sfc']}").alias("fuente_id"))
        if hist is None:
            csv = (respaldos or {}).get(s.get("respaldo_csv")) if respaldos is not None else _csv(s.get("respaldo_csv"))
            if csv is not None and not csv.is_empty():
                h = csv.filter(pl.col("fecha") <= tope)
                if inicio:
                    h = h.filter(pl.col("fecha") < inicio)
                hist = h.with_columns(
                    pl.lit(suc).alias("especie"), pl.lit(ant.get("nemotecnico")).alias("codigo_titulo"),
                    (pl.col("precio_promedio") / rel).alias("precio_promedio"),
                    pl.lit(f"sucesion:csv:{s.get('respaldo_csv')}").alias("fuente_id"),
                    pl.lit(None, pl.Datetime("us", "UTC")).alias("ingest_ts"))
        if hist is not None and not hist.is_empty():
            partes.append(hist.select([c for c in precios.columns]).cast(precios.schema))
    return pl.concat(partes).sort(["especie", "fecha"]) if len(partes) > 1 else precios


def precios_sfc() -> pl.DataFrame | None:
    """``silver/fact_precio_sfc`` con las sucesiones aplicadas."""
    precios = storage.leer_tabla("silver", "fact_precio_sfc")
    if precios is None or precios.is_empty():
        return precios
    return aplicar(precios)
