"""Salud de datos: último dato por fuente, rezago y estado de la última ejecución.

Alimenta la página "Salud de datos y señales" del tablero. Un dato con más rezago que el
permitido en config/convenciones.yaml se marca como vencido.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from invest import storage
from invest.calendario import sesiones_entre
from invest.config import catalogo_fuentes, convenciones


# Días hábiles de rezago normales según la frecuencia de publicación: un IPC mensual llega unos 5
# días hábiles después de terminar el mes siguiente (≈ 45 días hábiles desde la fecha del dato), y
# un 20-F anual hasta 4 meses después del cierre (≈ 1 año y 4 meses desde la fecha del dato).
TOLERANCIA_POR_FRECUENCIA = {"semanal": 10, "mensual": 50, "trimestral": 110, "semestral": 200, "anual": 360}


def _tolerancia(fuente: dict, limite_diario: int) -> int:
    if fuente.get("tolerancia_dias_habiles") is not None:
        return int(fuente["tolerancia_dias_habiles"])
    return TOLERANCIA_POR_FRECUENCIA.get(fuente.get("frecuencia"), limite_diario)


def evaluar(hoy: date | None = None) -> pl.DataFrame:
    hoy = hoy or date.today()
    limite = int(convenciones()["datos"]["dias_habiles_para_dato_vencido"])
    ejecuciones = storage.leer_ejecuciones()
    filas = []
    for f in catalogo_fuentes():
        if f.get("estado") != "implementado":
            continue
        tabla = f.get("tabla_destino")
        df = storage.leer_tabla("silver", tabla) if tabla else None
        ultimo = None
        if df is not None and not df.is_empty():
            sub = df.filter(pl.col("fuente_id") == f["id"]) if "fuente_id" in df.columns else df
            # Para la TRM, el último dato "de mercado" es la fecha_mercado más reciente; las fuentes
            # con otra columna de fecha la declaran en el catálogo (columna_fecha).
            col = f.get("columna_fecha") or ("fecha_mercado" if "fecha_mercado" in sub.columns else "fecha")
            ultimo = sub.get_column(col).max()
        rezago = None
        if ultimo is not None:
            rezago = max(len(sesiones_entre(ultimo, hoy)) - 1, 0) if ultimo <= hoy else 0
        ult_ej = next((e for e in reversed(ejecuciones) if e.get("fuente_id") == f["id"]), None)
        if ultimo is None:
            estado = "sin datos"
        elif ult_ej and ult_ej.get("estado") == "error":
            estado = "error en la última ejecución"
        elif rezago is not None and rezago > _tolerancia(f, limite):
            # Las fuentes manuales (las lanza Esteban) no cuentan como falla si no se leen a diario
            estado = "manual, sin leer reciente" if f.get("ejecuta_en") == "manual" else "vencido"
        else:
            estado = "al día"
        filas.append(
            {
                "fuente_id": f["id"],
                "fuente": f["nombre"],
                "ultimo_dato": ultimo,
                "rezago_dias_habiles": rezago,
                "ultima_ejecucion_utc": ult_ej.get("ts_utc") if ult_ej else None,
                "estado_ultima_ejecucion": ult_ej.get("estado") if ult_ej else None,
                "detalle": ult_ej.get("detalle") if ult_ej else None,
                "estado": estado,
            }
        )
    resultado = pl.DataFrame(
        filas,
        schema={
            "fuente_id": pl.Utf8, "fuente": pl.Utf8, "ultimo_dato": pl.Date,
            "rezago_dias_habiles": pl.Int64, "ultima_ejecucion_utc": pl.Utf8,
            "estado_ultima_ejecucion": pl.Utf8, "detalle": pl.Utf8, "estado": pl.Utf8,
        },
    )
    storage.escribir_completa("gold", "salud_datos", resultado)
    return resultado
