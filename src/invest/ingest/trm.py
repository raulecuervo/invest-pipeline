"""Conector de la TRM (Superintendencia Financiera, publicada en datos.gov.co).

Cada registro de la fuente trae un valor y un rango de vigencia (vigenciadesde..vigenciahasta) que
puede cubrir varios días: un viernes, por ejemplo, la misma TRM rige sábado, domingo y lunes festivo.
El conector expande cada rango a una fila por día calendario.

Convención (config/convenciones.yaml): la TRM vigente en el día t se calcula con las operaciones
del día hábil anterior. La columna ``fecha_mercado`` guarda ese día hábil, de modo que para
convertir un cierre del día t se busca la fila cuya ``fecha_mercado`` es t.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl

from invest.calendario import dia_habil_anterior
from invest.config import convenciones
from invest.ingest import socrata
from invest.ingest.base import Conector

ESQUEMA = {
    "fecha": pl.Date,
    "moneda_origen": pl.Utf8,
    "moneda_destino": pl.Utf8,
    "tasa": pl.Float64,
    "tipo": pl.Utf8,
    "vigencia_desde": pl.Date,
    "vigencia_hasta": pl.Date,
    "fecha_mercado": pl.Date,
    "fuente_id": pl.Utf8,
    "ingest_ts": pl.Datetime("us", "UTC"),
}


def _fecha(valor: str) -> date:
    return date.fromisoformat(valor[:10])


class ConectorTRM(Conector):
    fuente_id = "trm_datos_gov"
    tabla = "fact_fx_diaria"
    claves = ("fecha", "moneda_origen", "moneda_destino", "tipo")

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        where = (
            f"vigenciahasta >= '{desde.isoformat()}T00:00:00' "
            f"AND vigenciadesde <= '{hasta.isoformat()}T00:00:00'"
        )
        return socrata.consultar(self.meta["url"], where=where, order="vigenciadesde ASC")

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        ahora = datetime.now(timezone.utc)
        filas = []
        for r in registros:
            desde, hasta = _fecha(r["vigenciadesde"]), _fecha(r["vigenciahasta"])
            tasa = float(r["valor"])
            fecha_mercado = dia_habil_anterior(desde, "XBOG")
            d = desde
            while d <= hasta:
                filas.append(
                    {
                        "fecha": d,
                        "moneda_origen": "USD",
                        "moneda_destino": r.get("unidad", "COP"),
                        "tasa": tasa,
                        "tipo": "TRM",
                        "vigencia_desde": desde,
                        "vigencia_hasta": hasta,
                        "fecha_mercado": fecha_mercado,
                        "fuente_id": self.fuente_id,
                        "ingest_ts": ahora,
                    }
                )
                d += timedelta(days=1)
        if not filas:
            return pl.DataFrame(schema=ESQUEMA)
        df = pl.DataFrame(filas, schema=ESQUEMA)
        # Si dos registros se solapan, prevalece el de vigencia más reciente.
        return df.sort(["fecha", "vigencia_desde"]).unique(
            subset=["fecha", "moneda_origen", "moneda_destino", "tipo"], keep="last", maintain_order=True
        )

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Separa valores imposibles y posibles errores de digitación.

        Un movimiento grande que se sostiene es real (devaluación de 1998, crisis de 2008, marzo de
        2020) y se conserva. Lo sospechoso es un pico aislado: un valor que se aleja más del umbral
        respecto al anterior y al siguiente en sentidos opuestos. Se evalúa sobre los valores
        publicados (uno por vigencia), no sobre los días expandidos de fines de semana y festivos.
        """
        if df.is_empty():
            return df, df.with_columns(pl.lit("").alias("motivo")).head(0)
        datos = convenciones()["datos"]
        umbral = float(datos["salto_maximo_diario_trm"])
        extremo = float(datos.get("salto_extremo_trm", 0.15))
        publicados = (
            df.select("vigencia_desde", "tasa").unique().sort("vigencia_desde")
            .with_columns(
                (pl.col("tasa") / pl.col("tasa").shift(1) - 1).alias("_r_prev"),
                (pl.col("tasa").shift(-1) / pl.col("tasa") - 1).alias("_r_next"),
            )
            .with_columns(
                (
                    (pl.col("_r_prev").abs() > umbral) & (pl.col("_r_next").abs() > umbral)
                    & (pl.col("_r_prev").sign() != pl.col("_r_next").sign())
                ).fill_null(False).alias("_pico"),
                (pl.col("_r_prev").abs() > extremo).fill_null(False).alias("_extremo"),
            )
            .select("vigencia_desde", "tasa", "_pico", "_extremo")
        )
        df = df.join(publicados, on=["vigencia_desde", "tasa"], how="left")
        motivo = (
            pl.when(pl.col("tasa").is_null() | (pl.col("tasa") <= 0))
            .then(pl.lit("tasa no positiva"))
            .when(pl.col("vigencia_hasta") < pl.col("vigencia_desde"))
            .then(pl.lit("vigencia invertida"))
            .when(pl.col("_extremo"))
            .then(pl.lit(f"variación diaria mayor a {extremo:.0%}"))
            .when(pl.col("_pico"))
            .then(pl.lit(f"pico aislado mayor a {umbral:.0%} (sube y vuelve)"))
            .otherwise(pl.lit(None))
        )
        df = df.with_columns(motivo.alias("motivo")).drop("_pico", "_extremo").sort("fecha")
        cuarentena = df.filter(pl.col("motivo").is_not_null())
        validas = df.filter(pl.col("motivo").is_null()).drop("motivo")
        return validas, cuarentena


def huecos_calendario(df: pl.DataFrame) -> list[date]:
    """Días calendario sin TRM dentro del rango cubierto (la TRM debe cubrir todos los días)."""
    if df.is_empty():
        return []
    fechas = set(df.get_column("fecha").to_list())
    inicio, fin = min(fechas), max(fechas)
    faltantes, d = [], inicio
    while d <= fin:
        if d not in fechas:
            faltantes.append(d)
        d += timedelta(days=1)
    return faltantes
