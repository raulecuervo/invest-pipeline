"""Conector de SEC EDGAR: estados financieros XBRL de los emisores colombianos con ADR.

Usa la API oficial ``data.sec.gov/api/xbrl/companyfacts`` (todos los hechos XBRL de una empresa en
un JSON). Los emisores extranjeros reportan el 20-F en NIIF (taxonomía ``ifrs-full``), en pesos.
Se guardan todos los conceptos de ``ifrs-full`` y las acciones en circulación de ``dei``.

Cada hecho conserva el formulario, el número de radicación (``accn``) y la fecha de presentación
(``fecha_presentacion``): un mismo periodo reaparece como comparativo en el 20-F siguiente, y para
evitar sesgo de anticipación el análisis debe usar el dato disponible a cada fecha.

La SEC exige identificarse con un User-Agent que incluya un correo de contacto: variable de
entorno ``SEC_USER_AGENT`` (p. ej. "Nombre Apellido correo@dominio.com"). En GitHub Actions va como
secreto. Límite de la SEC: 10 consultas por segundo; aquí son tres o cuatro por corrida.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

import polars as pl

from invest.ingest.base import Conector

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{:010d}.json"
TAXONOMIAS = ("ifrs-full", "dei")

ESQUEMA = {
    "cik": pl.Int64, "emisor": pl.Utf8, "taxonomia": pl.Utf8, "concepto": pl.Utf8, "unidad": pl.Utf8,
    "tipo_periodo": pl.Utf8, "fecha_inicio": pl.Date, "fecha_fin": pl.Date, "valor": pl.Float64,
    "ejercicio": pl.Int64, "periodo_fiscal": pl.Utf8, "formulario": pl.Utf8, "fecha_presentacion": pl.Date,
    "accn": pl.Utf8, "frame": pl.Utf8, "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}


def hechos(companyfacts: dict, emisor: str) -> list[dict]:
    """Aplana el JSON de companyfacts a una fila por hecho."""
    cik = int(companyfacts["cik"])
    filas = []
    for tax in TAXONOMIAS:
        for concepto, d in (companyfacts.get("facts", {}).get(tax) or {}).items():
            for unidad, lista in (d.get("units") or {}).items():
                for f in lista:
                    fin = date.fromisoformat(f["end"])
                    inicio = date.fromisoformat(f["start"]) if f.get("start") else fin
                    filas.append({
                        "cik": cik, "emisor": emisor, "taxonomia": tax, "concepto": concepto, "unidad": unidad,
                        "tipo_periodo": "duracion" if f.get("start") else "instante",
                        "fecha_inicio": inicio, "fecha_fin": fin, "valor": f.get("val"),
                        "ejercicio": f.get("fy"), "periodo_fiscal": f.get("fp"), "formulario": f.get("form"),
                        "fecha_presentacion": date.fromisoformat(f["filed"]) if f.get("filed") else None,
                        "accn": f.get("accn"), "frame": f.get("frame"),
                    })
    return filas


class ConectorEDGAR(Conector):
    fuente_id = "sec_edgar"
    tabla = "fact_xbrl_sec"
    claves = ("cik", "taxonomia", "concepto", "unidad", "fecha_inicio", "fecha_fin", "accn")
    columna_fecha = "fecha_fin"

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        agente = os.environ.get("SEC_USER_AGENT", "").strip()
        if "@" not in agente:
            raise ValueError("Falta SEC_USER_AGENT con un correo de contacto (la SEC lo exige)")
        registros = []
        for em in self.meta["emisores"]:
            for cik in em["cik"]:
                contenido = self.bajar(URL.format(int(cik)), cabeceras={"User-Agent": agente}, timeout=120)
                if contenido is None:
                    continue
                self.crudos[f"CIK{int(cik):010d}.json"] = contenido
                registros += hechos(json.loads(contenido), em["nombre"])
        if not registros:
            self.sin_novedades = True
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA)
        ahora = datetime.now(timezone.utc)
        df = pl.DataFrame([{**r, "fuente_id": self.fuente_id, "ingest_ts": ahora} for r in registros], schema=ESQUEMA)
        return df.unique(subset=list(self.claves), keep="last", maintain_order=True)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        motivo = (pl.when(pl.col("valor").is_null()).then(pl.lit("valor vacío"))
                  .when(pl.col("accn").is_null()).then(pl.lit("sin número de radicación"))
                  .otherwise(pl.lit(None)))
        df = df.with_columns(motivo.alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
