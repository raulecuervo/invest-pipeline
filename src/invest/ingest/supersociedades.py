"""Estados financieros anuales de respaldo: "10.000 empresas más grandes del país" (Supersociedades).

Conjunto ``6cat-2gcs`` de datos.gov.co. Supersociedades lo arma cada año (fin de junio) con lo que
las compañías reportan a ella y a otras superintendencias, incluida la Superintendencia Financiera
para las del sector real inscritas en el RNVE. Trae, por NIT y año de corte (31 de diciembre):
ingresos operacionales, ganancia (pérdida), activos, pasivos y patrimonio.

Para qué sirve aquí:

- **Respaldo** de los emisores sin XBRL en el SIMEV (26 de 36 en nov-2026): da activos, pasivos,
  patrimonio, ingresos y utilidad del año, que los anexos de la SFC no traen (o traen mal: los
  "ingresos" del anexo 141 no son los ingresos operacionales).
- **Control**: el patrimonio separado debe ser igual al valor patrimonial por acción × acciones en
  circulación de los anexos (``invest.fundamentales.cotejo``).

Límites, que quedan marcados en ``gold/fact_fundamental`` (``base = separado_anual``):

- Cifras en **billones de pesos con dos decimales**: precisión de ± 5.000 millones. Sirve para
  emisores grandes; en uno pequeño la utilidad puede aparecer como 0,00.
- Estados **separados** (individuales), no consolidados. En una holding el separado es otra cosa.
- Solo **cierres anuales** desde 2021 y solo las empresas que entran en el ranking (los bancos no:
  para ellos está el CUIF).
- Se publica a fines de junio del año siguiente: esa es su ``fecha_disponible``.

Se pide solo lo de los NIT de ``config/emisores_rnve.yaml`` (sin dígito de verificación).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

import polars as pl

from invest.ingest import socrata
from invest.ingest.base import Conector

ESQUEMA = {
    "nit": pl.Utf8, "emisor_id": pl.Utf8, "razon_social": pl.Utf8, "supervisor": pl.Utf8, "anio": pl.Int32,
    "fecha_corte": pl.Date, "ciiu": pl.Utf8, "macrosector": pl.Utf8,
    "ingresos_operacionales": pl.Float64, "ganancia": pl.Float64, "activos": pl.Float64,
    "pasivos": pl.Float64, "patrimonio": pl.Float64, "precision_pesos": pl.Float64,
    "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}
BILLON = 1e12
PRECISION = 0.005 * BILLON  # dos decimales en billones
CAMPOS = {"ingresos_operacionales": "ingresos_operacionales", "ganancia_p_rdida": "ganancia",
          "total_activos": "activos", "total_pasivos": "pasivos", "total_patrimonio": "patrimonio"}


def billones(texto: object) -> float | None:
    """'$ 198.11' → 198.11e12; '-$ 0.04' → -0.04e12; '' → None."""
    if texto is None:
        return None
    t = re.sub(r"[\s$]", "", str(texto)).replace(",", "")
    if t in ("", "-"):
        return None
    try:
        return round(float(t) * 100) * (BILLON / 100)  # centésimas de billón exactas
    except ValueError:
        return None


def nit_base(nit: object) -> str:
    """'899999068.00' → '899999068'."""
    return str(nit).split(".")[0].strip()


def nits_emisores() -> dict[str, str]:
    """NIT sin dígito de verificación → emisor_id, de config/emisores_rnve.yaml."""
    from invest.ingest.sfc_niif import emisores

    salida = {}
    for e in emisores():
        nit = re.sub(r"\D", "", str(e.get("nit", "")))
        if len(nit) >= 9:
            salida[nit[:9]] = e["emisor_id"]
    return salida


class ConectorSupersociedades(Conector):
    fuente_id = "supersociedades_10000"
    tabla = "fact_ee_anual_supersociedades"
    claves = ("nit", "anio")
    columna_fecha = "fecha_corte"

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        nits = nits_emisores()
        lista = ",".join(f"'{n}.00'" for n in sorted(nits))
        where = f"nit in({lista})"
        registros = socrata.consultar(self.meta["url"], where=where, order="a_o_de_corte,nit", timeout=120)
        return [r for r in registros
                if desde.year <= int(float(r.get("a_o_de_corte") or 0)) <= hasta.year]

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA)
        mapa = nits_emisores()
        ahora = datetime.now(timezone.utc)
        filas = []
        for r in registros:
            anio = int(float(r["a_o_de_corte"]))
            nit = nit_base(r["nit"])
            fila = {"nit": nit, "emisor_id": mapa.get(nit), "razon_social": r.get("raz_n_social"),
                    "supervisor": r.get("supervisor"), "anio": anio, "fecha_corte": date(anio, 12, 31),
                    "ciiu": nit_base(r.get("ciiu", "")) or None, "macrosector": r.get("macrosector"),
                    "precision_pesos": PRECISION, "fuente_id": self.fuente_id, "ingest_ts": ahora}
            for campo, destino in CAMPOS.items():
                fila[destino] = billones(r.get(campo))
            filas.append(fila)
        return pl.DataFrame(filas, schema=ESQUEMA).unique(subset=list(self.claves), keep="last", maintain_order=True)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        if df.is_empty():
            return df, df.with_columns(pl.lit("").alias("motivo")).head(0)
        # Activo = pasivo + patrimonio, con la tolerancia del redondeo a dos decimales (3 × 0,005 billones)
        descuadre = (pl.col("activos") - pl.col("pasivos") - pl.col("patrimonio")).abs() > 3 * PRECISION + 1
        df = df.with_columns(
            pl.when(pl.col("emisor_id").is_null()).then(pl.lit("NIT sin emisor"))
            .when(pl.col("activos").is_null() | (pl.col("activos") <= 0)).then(pl.lit("activos vacíos"))
            .when(descuadre).then(pl.lit("no cuadra activo = pasivo + patrimonio"))
            .otherwise(pl.lit(None)).alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
