"""Tasas de CDT y fondos de inversión colectiva de la Superintendencia Financiera (datos.gov.co).

**CDT** (``cdt_datos_gov``, dataset axk9-g2nh: informe diario de tasas de captación, formato 441).
Cada entidad reporta, por día, la tasa y el monto de sus emisiones de CDT por plazo (unidad de
captura 1). Se guarda por día el promedio de la tasa ponderado por el monto, para los plazos de 90,
180 y 360 días (subcuentas 70, 110 y 130), como series ``sfc_cdt_90``, ``sfc_cdt_180`` y
``sfc_cdt_360`` de ``silver/fact_serie_macro`` (tasa efectiva anual en %). Es lo que pagaron los CDT
nuevos ese día en todo el sistema; la DTF del Banco de la República es el mismo cálculo a 90 días,
semanal. Desde 2018 (inicio del dataset).

**FIC** (``fic_datos_gov``, dataset qhpu-8ixx: formato 523, un registro por fondo, tipo de
participación y día). Para el Laboratorio basta el valor de la unidad a fin de mes (ya neto de la
comisión de administración), así que solo se piden los últimos días de cada mes y las columnas que
se usan: unas 2.500 filas por mes, contra millones si se bajara cada día. Van a
``silver/fact_fic_mensual``. Desde 2016.

Ambas solo bajan lo nuevo: los días (o meses) desde el último guardado, menos el margen de
``recarga_dias`` del catálogo.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

import polars as pl

from invest.ingest import socrata
from invest.ingest.base import Conector
from invest.ingest.dane_ipc import ESQUEMA

PLAZOS_CDT = {"70": 90, "110": 180, "130": 360}  # subcuenta de la unidad de captura 1 → días


def _numero(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def promedios_cdt(registros: list[dict]) -> list[dict]:
    """Tasa promedio ponderada por monto, por día y plazo. Ignora montos nulos o cero y tasas fuera de
    0–50 % (errores de digitación)."""
    acumulado: dict[tuple[str, int], list[float]] = {}
    for r in registros:
        plazo = PLAZOS_CDT.get(str(r.get("subcuenta")))
        tasa, monto = _numero(r.get("tasa")), _numero(r.get("monto"))
        if plazo is None or tasa is None or monto is None or monto <= 0 or not 0 < tasa < 50:
            continue
        clave = (str(r["fechacorte"])[:10], plazo)
        a = acumulado.setdefault(clave, [0.0, 0.0, 0.0])
        a[0] += tasa * monto
        a[1] += monto
        a[2] += 1
    return [{"fecha": f, "plazo": p, "tasa": s / m, "monto": m, "reportes": int(n)}
            for (f, p), (s, m, n) in sorted(acumulado.items())]


class ConectorCDT(Conector):
    fuente_id = "cdt_datos_gov"
    tabla = "fact_serie_macro/fuente=cdt_datos_gov"
    claves = ("fecha", "serie_id")

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        subcuentas = ",".join(f"'{s}'" for s in PLAZOS_CDT)
        where = (f"uca = '1' AND subcuenta in ({subcuentas}) AND fechacorte >= '{desde.isoformat()}T00:00:00' "
                 f"AND fechacorte <= '{hasta.isoformat()}T00:00:00'")
        crudos = socrata.consultar(self.meta["url"], where=where, order="fechacorte ASC")
        # Se guarda el promedio del día, no cada reporte (el crudo de una carga completa pesa cientos de MB)
        registros = promedios_cdt(crudos)
        if not registros:
            self.sin_novedades = True
        else:
            self.notas.append(f"{len(crudos)} reportes de {len({r['fecha'] for r in registros})} días")
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA)
        ahora = datetime.now(timezone.utc)
        return pl.DataFrame([{
            "fecha": date.fromisoformat(r["fecha"]), "serie_id": f"sfc_cdt_{r['plazo']}", "valor": float(r["tasa"]),
            "unidad": "pct_ea", "frecuencia": "diaria", "fecha_publicacion": None, "publicacion_estimada": None,
            "fuente_id": self.fuente_id, "ingest_ts": ahora} for r in registros], schema=ESQUEMA)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        motivo = pl.when(pl.col("valor").is_nan() | (pl.col("valor") <= 0)).then(pl.lit("tasa no válida"))
        df = df.with_columns(motivo.otherwise(pl.lit(None)).alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())


# --------------------------------------------------------------------------- FIC
ESQUEMA_FIC = {
    "fecha": pl.Date, "codigo_entidad": pl.Utf8, "nombre_entidad": pl.Utf8, "codigo_negocio": pl.Utf8,
    "nombre_fondo": pl.Utf8, "subtipo": pl.Utf8, "tipo_participacion": pl.Utf8, "valor_unidad": pl.Float64,
    "valor_fondo": pl.Float64, "inversionistas": pl.Int64, "rentabilidad_mensual": pl.Float64,
    "rentabilidad_anual": pl.Float64, "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}
COLUMNAS_FIC = ("fecha_corte,codigo_entidad,nombre_entidad,codigo_negocio,nombre_patrimonio,nombre_subtipo_patrimonio,"
                "tipo_participacion,valor_unidad_operaciones,valor_fondo_cierre_dia_t,numero_inversionistas,"
                "rentabilidad_mensual,rentabilidad_anual")
DIAS_AL_CIERRE = 4  # se piden los últimos días de cada mes: algunos fondos no reportan el último


def ultimos_dias(desde: date, hasta: date) -> list[date]:
    """Los últimos ``DIAS_AL_CIERRE`` días de cada mes cerrado entre las dos fechas."""
    dias, y, m = [], desde.year, desde.month
    while (y, m) <= (hasta.year, hasta.month):
        fin = date(y, m, calendar.monthrange(y, m)[1])
        if fin <= hasta:
            dias += [fin - timedelta(days=k) for k in range(DIAS_AL_CIERRE)]
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(d for d in dias if d >= desde)


def cierre_de_mes(registros: list[dict]) -> list[dict]:
    """De los últimos días de cada mes, el último reporte de cada fondo y participación."""
    mejor: dict[tuple, dict] = {}
    for r in registros:
        f = str(r.get("fecha_corte", ""))[:10]
        if not f or _numero(r.get("valor_unidad_operaciones")) in (None, 0):
            continue
        clave = (f[:7], str(r.get("codigo_entidad")), str(r.get("codigo_negocio")), str(r.get("tipo_participacion")))
        if clave not in mejor or f > str(mejor[clave]["fecha_corte"])[:10]:
            mejor[clave] = r
    return list(mejor.values())


class ConectorFIC(Conector):
    fuente_id = "fic_datos_gov"
    tabla = "fact_fic_mensual"
    claves = ("fecha", "codigo_entidad", "codigo_negocio", "tipo_participacion")

    def rango_por_defecto(self, hoy: date | None = None) -> tuple[date, date]:
        desde, _ = super().rango_por_defecto(hoy)
        hoy = hoy or date.today()
        return desde, hoy  # solo meses cerrados (ultimos_dias descarta el mes en curso)

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        dias = ultimos_dias(desde, hasta)
        crudos: list[dict] = []
        for k in range(0, len(dias), 12):  # tres meses por consulta (URL corta)
            lista = ",".join(f"'{d.isoformat()}T00:00:00'" for d in dias[k:k + 12])
            crudos += socrata.consultar(self.meta["url"], where=f"fecha_corte in ({lista})",
                                        order="fecha_corte ASC", select=COLUMNAS_FIC)
        registros = cierre_de_mes(crudos)
        if not registros:
            self.sin_novedades = True
        else:
            self.notas.append(f"{len({r['fecha_corte'][:7] for r in registros})} meses, "
                              f"{len({(r['codigo_entidad'], r['codigo_negocio']) for r in registros})} fondos")
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA_FIC)
        ahora = datetime.now(timezone.utc)

        def entero(x):
            v = _numero(x)
            return None if v is None else int(v)

        return pl.DataFrame([{
            "fecha": date.fromisoformat(str(r["fecha_corte"])[:10]), "codigo_entidad": str(r.get("codigo_entidad")),
            "nombre_entidad": r.get("nombre_entidad"), "codigo_negocio": str(r.get("codigo_negocio")),
            "nombre_fondo": r.get("nombre_patrimonio"), "subtipo": r.get("nombre_subtipo_patrimonio"),
            "tipo_participacion": str(r.get("tipo_participacion")), "valor_unidad": _numero(r.get("valor_unidad_operaciones")),
            "valor_fondo": _numero(r.get("valor_fondo_cierre_dia_t")), "inversionistas": entero(r.get("numero_inversionistas")),
            "rentabilidad_mensual": _numero(r.get("rentabilidad_mensual")), "rentabilidad_anual": _numero(r.get("rentabilidad_anual")),
            "fuente_id": self.fuente_id, "ingest_ts": ahora} for r in registros], schema=ESQUEMA_FIC)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        motivo = pl.when(pl.col("valor_unidad").is_null() | (pl.col("valor_unidad") <= 0)).then(pl.lit("valor de unidad no válido"))
        df = df.with_columns(motivo.otherwise(pl.lit(None)).alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
