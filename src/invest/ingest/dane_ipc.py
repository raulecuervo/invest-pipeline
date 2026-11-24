"""Conector del IPC del DANE (índice total nacional, serie de empalme, base dic-2018 = 100).

El DANE no tiene API para el IPC. Cada mes publica en su página un anexo .xlsx con la serie de
empalme desde 2003, con una URL que cambia con el mes (``.../IPC/oct2026/anex-IPC-Indices-oct2026.xlsx``).
El conector lee la página, toma el enlace vigente y descarga el anexo solo si cambió.

Para evitar sesgo de anticipación, cada mes lleva ``fecha_publicacion``: la del aviso
"Actualizado el …" del anexo para el último mes, y para los anteriores una estimación (quinto día
hábil del mes siguiente, cuando el DANE suele publicar), marcada en ``publicacion_estimada``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import polars as pl

from invest.calendario import dia_habil_siguiente_o_mismo
from invest.ingest.base import Conector
from invest.ingest.xlsx import leer_hoja

MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}

ESQUEMA = {
    "fecha": pl.Date, "serie_id": pl.Utf8, "valor": pl.Float64, "unidad": pl.Utf8, "frecuencia": pl.Utf8,
    "fecha_publicacion": pl.Date, "publicacion_estimada": pl.Boolean, "fuente_id": pl.Utf8,
    "ingest_ts": pl.Datetime("us", "UTC"),
}

_ENLACE = re.compile(rb'href="([^"]*anex-IPC-Indices-[a-z]{3}\d{4}\.xlsx)"', re.I)
_ACTUALIZADO = re.compile(r"actualizado el (\d{1,2}) de ([a-záéíóú]+) de (\d{4})", re.I)


def leer_indices(contenido: bytes) -> tuple[list[dict], date | None]:
    """Registros {año, mes, valor} del anexo y la fecha de "Actualizado el …"."""
    filas = leer_hoja(contenido)
    encabezado, anios = None, {}
    actualizado = None
    registros = []
    for i, fila in enumerate(filas):
        primera = fila[0] if fila else None
        if isinstance(primera, str):
            m = _ACTUALIZADO.search(primera)
            if m and m.group(2).lower() in MESES:
                actualizado = date(int(m.group(3)), MESES[m.group(2).lower()], int(m.group(1)))
        if encabezado is None and isinstance(primera, str) and primera.strip().lower() == "mes":
            encabezado = i
            anios = {j: int(float(v)) for j, v in enumerate(fila)
                     if isinstance(v, (float, str)) and str(v).replace(".0", "").strip().isdigit()
                     and 1950 < float(v) < 2200}
            continue
        if encabezado is not None and isinstance(primera, str) and primera.strip().lower() in MESES:
            mes = MESES[primera.strip().lower()]
            for j, anio in anios.items():
                v = fila[j] if j < len(fila) else None
                if isinstance(v, float):
                    registros.append({"anio": anio, "mes": mes, "valor": v})
    if encabezado is None:
        raise ValueError("El anexo del IPC no tiene la fila de encabezado 'Mes' con los años")
    return registros, actualizado


def quinto_dia_habil(anio: int, mes: int) -> date:
    d = dia_habil_siguiente_o_mismo(date(anio, mes, 1))
    for _ in range(4):
        d = dia_habil_siguiente_o_mismo(d + timedelta(days=1))
    return d


class ConectorDANEIPC(Conector):
    fuente_id = "dane_ipc"
    tabla = "fact_serie_macro"
    claves = ("fecha", "serie_id")

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        pagina = self.bajar(self.meta["url"])
        if pagina is None:
            self.sin_novedades = True
            return []
        m = _ENLACE.search(pagina)
        if not m:
            raise ValueError("No se encontró el enlace al anexo de índices en la página del IPC del DANE")
        url = urljoin(self.meta["url"], m.group(1).decode())
        contenido = self.bajar(url)
        if contenido is None:
            self.sin_novedades = True
            return []
        self.crudos[url.rsplit("/", 1)[-1]] = contenido
        registros, actualizado = leer_indices(contenido)
        for r in registros:
            r["actualizado"] = actualizado
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA)
        ahora = datetime.now(timezone.utc)
        ultimo = max((r["anio"], r["mes"]) for r in registros)
        filas = []
        for r in registros:
            fin = (r["anio"], r["mes"]) == ultimo and r.get("actualizado")
            sig = (r["anio"] + (r["mes"] == 12), r["mes"] % 12 + 1)
            filas.append({
                "fecha": date(r["anio"], r["mes"], 1), "serie_id": "ipc_co", "valor": r["valor"],
                "unidad": "indice_dic2018_100", "frecuencia": "mensual",
                "fecha_publicacion": r["actualizado"] if fin else quinto_dia_habil(*sig),
                "publicacion_estimada": not fin, "fuente_id": self.fuente_id, "ingest_ts": ahora,
            })
        return pl.DataFrame(filas, schema=ESQUEMA).sort("fecha")

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        if df.is_empty():
            return df, df.with_columns(pl.lit("").alias("motivo")).head(0)
        df = df.sort("fecha").with_columns((pl.col("valor") / pl.col("valor").shift(1) - 1).abs().alias("_v"))
        motivo = (pl.when(pl.col("valor") <= 0).then(pl.lit("índice no positivo"))
                  .when(pl.col("_v") > 0.05).then(pl.lit("variación mensual mayor a 5 %"))
                  .otherwise(pl.lit(None)))
        df = df.with_columns(motivo.alias("motivo")).drop("_v")
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
