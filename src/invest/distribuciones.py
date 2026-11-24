"""Distribuciones en especie: escisiones y repartos de acciones de otra sociedad.

Lee la lista curada de ``config/eventos_capital.yaml`` (relación de reparto oficial y su fuente) y
calcula cuánto vale lo que recibe cada acción:

    valor_por_accion = Σ acciones_por_accion × precio de la acción recibida

El precio de la acción recibida es el promedio ponderado de la SFC en la primera rueda con
negociación desde ``fecha_ex`` (hasta ``ventana_precio_dias`` días después). Si no negoció en ese
lapso (debut tardío, o la acción no está en el universo SFC) se usa ``precio_referencia`` del
evento, y la fila queda marcada con ``origen_precio = "referencia"``.

El resultado va a gold/fact_distribucion_especie y ``retornos.py`` lo suma al retorno total del
primer día negociado desde ``fecha_ex``.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import yaml

from invest import storage
from invest.config import CONFIG_DIR

ESQUEMA = {
    "especie": pl.Utf8, "fecha_ex": pl.Date, "tipo": pl.Utf8, "especie_recibida": pl.Utf8,
    "nombre_recibida": pl.Utf8, "acciones_por_accion": pl.Float64, "precio_recibida": pl.Float64,
    "fecha_precio": pl.Date, "origen_precio": pl.Utf8, "valor": pl.Float64, "fuente": pl.Utf8,
}


def config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "eventos_capital.yaml").read_text(encoding="utf-8"))


def _fecha(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def negociados(precios: pl.DataFrame) -> pl.DataFrame:
    """Solo los días con negociación real.

    La SFC repite el último precio los días sin negociación (valor transado 0). Esos días no son
    observaciones de mercado: contarlos como retorno cero subestima la volatilidad. Se conservan
    las pocas filas con valor 0 pero precio distinto al anterior (montos ínfimos redondeados).
    """
    return precios.sort(["especie", "fecha"]).filter(
        (pl.col("valor_transado") > 0)
        | (pl.col("precio_promedio") != pl.col("precio_promedio").shift(1).over("especie")).fill_null(True)
    )


def _precio(precios: dict[str, list[tuple[date, float]]], especie: str | None, desde: date, dias: int):
    if not especie:
        return None
    hasta = desde + timedelta(days=dias)
    for f, p in precios.get(especie, []):
        if desde <= f <= hasta:
            return f, p
        if f > hasta:
            break
    return None


def construir() -> tuple[pl.DataFrame, dict]:
    """Escribe gold/fact_distribucion_especie. Devuelve (tabla, resumen)."""
    cfg = config()
    dias = int(cfg.get("ventana_precio_dias", 10))
    eventos = cfg.get("eventos") or []
    resumen = {"eventos": len(eventos), "precio_mercado": 0, "precio_referencia": 0, "sin_precio": 0}
    crudo = storage.leer_tabla("silver", "fact_precio_sfc")
    por_especie: dict[str, list[tuple[date, float]]] = {}
    if crudo is not None and not crudo.is_empty():
        for e, grupo in negociados(crudo.select("fecha", "especie", "precio_promedio", "valor_transado")) \
                .group_by("especie"):
            por_especie[e[0]] = list(grupo.sort("fecha").select("fecha", "precio_promedio").iter_rows())

    filas = []
    for ev in eventos:
        fecha_ex = _fecha(ev["fecha_ex"])
        for ent in ev.get("entrega") or []:
            encontrado = _precio(por_especie, ent.get("especie"), fecha_ex, dias)
            if encontrado:
                fecha_precio, precio, origen = encontrado[0], float(encontrado[1]), "mercado"
            elif ent.get("precio_referencia") is not None:
                fecha_precio, precio, origen = None, float(ent["precio_referencia"]), "referencia"
            else:
                resumen["sin_precio"] += 1
                continue
            resumen["precio_" + origen] += 1
            k = float(ent["acciones_por_accion"])
            filas.append({
                "especie": ev["especie"], "fecha_ex": fecha_ex, "tipo": ev.get("tipo", "escision"),
                "especie_recibida": ent.get("especie"), "nombre_recibida": ent.get("nombre"),
                "acciones_por_accion": k, "precio_recibida": precio, "fecha_precio": fecha_precio,
                "origen_precio": origen, "valor": k * precio, "fuente": ev.get("fuente"),
            })
    tabla = pl.DataFrame(filas, schema=ESQUEMA)
    storage.escribir_completa("gold", "fact_distribucion_especie", tabla)
    resumen["filas"] = tabla.height
    return tabla, resumen


def por_especie_y_fecha() -> pl.DataFrame:
    """Valor entregado por acción, sumado por especie y fecha_ex (lo que usa retornos.py)."""
    t = storage.leer_tabla("gold", "fact_distribucion_especie")
    if t is None or t.is_empty():
        return pl.DataFrame(schema={"especie": pl.Utf8, "fecha_ex": pl.Date, "distribucion": pl.Float64})
    return t.group_by(["especie", "fecha_ex"]).agg(pl.col("valor").sum().alias("distribucion"))
