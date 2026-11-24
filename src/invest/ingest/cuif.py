"""Conector del CUIF mensual de los bancos (Superintendencia Financiera, en datos.gov.co).

El CUIF (Catálogo Único de Información Financiera con fines de supervisión) trae el balance y el
estado de resultados de cada entidad vigilada, cada mes, por cuenta. Para el análisis basta el
primer nivel de cuentas (código ``XX0000``: activo, cartera, depósitos, patrimonio, ingresos,
gastos, utilidad), en moneda total, de los bancos con acciones en la Bolsa de Colombia
(``entidades`` en el catálogo). El cruce con los estados NIIF trimestrales sirve de control.

Los saldos de resultados (clases 4 y 5, y la cuenta 590000) son acumulados del año: el de marzo
cubre enero a marzo. La API es lenta con este conjunto (unos 40 segundos por consulta), así que
el tiempo de espera es largo.

Además del primer nivel pide las ``cuentas_extra`` del catálogo (las de la plantilla de bancos,
config/bancos.yaml). Cuando la lista trae cuentas que nunca se bajaron (se guarda en
meta/estado_fuentes/cuif_datos_gov.json), la corrida sin ``--desde`` vuelve a pedir todo el histórico
una sola vez, para que las cuentas nuevas no queden solo con los últimos meses.

Validación: activo = pasivo + patrimonio en cada entidad y mes (tolerancia 1 %). Si no cuadra, las
filas de ese mes van a cuarentena.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl

from invest import storage
from invest.ingest import socrata
from invest.ingest.base import Conector

ESQUEMA = {
    "fecha_corte": pl.Date, "codigo_entidad": pl.Utf8, "nombre_entidad": pl.Utf8, "cuenta": pl.Utf8,
    "nombre_cuenta": pl.Utf8, "valor": pl.Float64, "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}


class ConectorCUIF(Conector):
    fuente_id = "cuif_datos_gov"
    tabla = "fact_cuif_mensual"
    claves = ("fecha_corte", "codigo_entidad", "cuenta")
    columna_fecha = "fecha_corte"

    def _extra(self) -> list[str]:
        return [str(c) for c in (self.meta.get("cuentas_extra") or [])]

    def rango_por_defecto(self, hoy: date | None = None) -> tuple[date, date]:
        desde, hasta = super().rango_por_defecto(hoy)
        bajadas = set(storage.leer_estado_fuente(self.fuente_id).get("cuentas_extra") or [])
        if set(self._extra()) - bajadas:  # cuentas nuevas en el catálogo: todo el histórico, una vez
            desde = min(desde, date.fromisoformat(str(self.meta.get("historico_desde", "2000-01-01"))))
        return desde, hasta

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        codigos = ",".join(f"'{e['codigo']}'" for e in self.meta["entidades"])
        extra = self._extra()
        cuentas = "cuenta like '__0000'" + (
            " OR cuenta in(" + ",".join(f"'{c}'" for c in extra) + ")" if extra else "")
        where = (
            f"tipo_entidad='1' AND codigo_entidad in({codigos}) AND moneda='0' AND ({cuentas}) "
            f"AND fecha_corte >= '{desde.isoformat()}T00:00:00' AND fecha_corte <= '{hasta.isoformat()}T00:00:00'"
        )
        registros = socrata.consultar(self.meta["url"], where=where, order="fecha_corte,codigo_entidad,cuenta",
                                      timeout=240)
        if desde <= date.fromisoformat(str(self.meta.get("historico_desde", "2000-01-01"))):
            self.estado_nuevo["cuentas_extra"] = sorted(extra)  # se guarda solo si la corrida termina bien
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA)
        ahora = datetime.now(timezone.utc)
        filas = []
        for r in registros:
            valor = float(r["valor"]) if r.get("valor") not in (None, "") else None
            if valor is not None and r.get("signo_valor") == "-":
                valor = -abs(valor)
            filas.append({
                "fecha_corte": date.fromisoformat(r["fecha_corte"][:10]), "codigo_entidad": r["codigo_entidad"],
                "nombre_entidad": r.get("nombre_entidad"), "cuenta": r["cuenta"],
                "nombre_cuenta": r.get("nombre_cuenta"), "valor": valor,
                "fuente_id": self.fuente_id, "ingest_ts": ahora,
            })
        return pl.DataFrame(filas, schema=ESQUEMA).unique(
            subset=list(self.claves), keep="last", maintain_order=True)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        if df.is_empty():
            return df, df.with_columns(pl.lit("").alias("motivo")).head(0)
        llave = ["fecha_corte", "codigo_entidad"]
        cuadre = df.group_by(llave).agg(
            pl.col("valor").filter(pl.col("cuenta") == "100000").first().alias("_a"),
            pl.col("valor").filter(pl.col("cuenta") == "200000").first().alias("_p"),
            pl.col("valor").filter(pl.col("cuenta") == "300000").first().alias("_k"),
        ).with_columns(
            ((pl.col("_a") - pl.col("_p") - pl.col("_k")).abs() > 0.01 * pl.col("_a").abs()).fill_null(False)
            .alias("_descuadre")
        ).select(*llave, "_descuadre")
        df = df.join(cuadre, on=llave, how="left").with_columns(
            pl.when(pl.col("valor").is_null()).then(pl.lit("valor vacío"))
            .when(pl.col("_descuadre")).then(pl.lit("no cuadra activo = pasivo + patrimonio"))
            .otherwise(pl.lit(None)).alias("motivo")
        ).drop("_descuadre")
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
