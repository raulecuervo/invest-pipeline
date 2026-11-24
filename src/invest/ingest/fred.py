"""Conector de FRED (Reserva Federal de St. Louis): series internacionales de respaldo.

Usa la descarga CSV pública (``fredgraph.csv?id=…``), que no exige clave. Cada serie del catálogo
(config/catalogo_fuentes.yaml, campo ``series``) se baja completa solo si cambió. Los valores
faltantes de FRED vienen como "." y se omiten.

Uso: tasa libre de riesgo de EE. UU. (bonos del Tesoro a 3 meses y 10 años, y el real a 10 años),
inflación implícita y de EE. UU., la tasa larga de Colombia (OCDE), el VIX y el diferencial de
crédito corporativo de mercados emergentes. Términos de FRED: el repo de datos es privado y no se
redistribuye (``redistribuible: false``).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl

from invest.ingest.base import Conector
from invest.ingest.dane_ipc import ESQUEMA

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"


def leer_csv(contenido: bytes, serie: str) -> list[dict]:
    lineas = contenido.decode("utf-8-sig").strip().splitlines()
    if not lineas or not lineas[0].lower().startswith(("observation_date", "date")):
        raise ValueError(f"FRED {serie}: respuesta sin el encabezado esperado")
    registros = []
    for linea in lineas[1:]:
        partes = linea.split(",")
        if len(partes) < 2 or partes[1].strip() in ("", "."):
            continue
        registros.append({"serie": serie, "fecha": partes[0].strip(), "valor": partes[1].strip()})
    return registros


class ConectorFRED(Conector):
    fuente_id = "fred"
    # Una carpeta por fuente dentro de la tabla: así GitHub Actions y el PC nunca escriben el mismo
    # archivo (leer_tabla("silver", "fact_serie_macro") las une)
    tabla = "fact_serie_macro/fuente=fred"
    claves = ("fecha", "serie_id")

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        registros = []
        self.series = {s["id"]: s for s in self.meta["series"]}
        fallidas = []
        for sid in self.series:
            # FRED a veces tarda en responder a los servidores de GitHub: más tiempo de espera, y una serie
            # que falla no impide guardar las demás (se reintenta en la próxima corrida)
            try:
                contenido = self.bajar(URL.format(sid), timeout=120)
            except Exception as e:  # noqa: BLE001
                fallidas.append(f"{sid} ({type(e).__name__})")
                continue
            if contenido is None:
                continue
            self.crudos[f"{sid}.csv"] = contenido
            registros += leer_csv(contenido, sid)
        if fallidas:
            if len(fallidas) == len(self.series):
                raise RuntimeError("no respondió ninguna serie de FRED: " + ", ".join(fallidas[:3]))
            self.notas.append(f"series sin respuesta {len(fallidas)} (se reintentan mañana): " + ", ".join(fallidas))
        if not registros:
            self.sin_novedades = True
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA)
        ahora = datetime.now(timezone.utc)
        series = getattr(self, "series", {s["id"]: s for s in self.meta["series"]})
        return pl.DataFrame([{
            "fecha": date.fromisoformat(r["fecha"]), "serie_id": "fred_" + r["serie"].lower(),
            "valor": float(r["valor"]), "unidad": series.get(r["serie"], {}).get("unidad"),
            "frecuencia": series.get(r["serie"], {}).get("frecuencia"), "fecha_publicacion": None,
            "publicacion_estimada": None, "fuente_id": self.fuente_id, "ingest_ts": ahora,
        } for r in registros], schema=ESQUEMA)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        motivo = pl.when(pl.col("valor").is_nan() | pl.col("valor").is_infinite()).then(pl.lit("valor no numérico"))
        df = df.with_columns(motivo.otherwise(pl.lit(None)).alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
