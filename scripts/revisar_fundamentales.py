"""Lista las cifras por acción que siguen raras después de los filtros de src/invest/fundamentales.py.

Uso (en el PC, después de ``uv run invest modelo``):

    uv run python scripts/revisar_fundamentales.py            # todo
    uv run python scripts/revisar_fundamentales.py --csv x.csv # además, en un CSV

No cambia nada: es una lista para revisar a mano con los PDF o la prensa. Tres revisiones:

1. Valor patrimonial por acción que salta más de 40 % entre cortes seguidos (separado) sin split entre
   medio. Una escisión de verdad sale aquí también (Bogotá mar-2022, BHI); el resto suele ser escala.
2. Utilidad por acción acumulada que baja dentro del año estando positiva en los dos cortes: o hubo un
   trimestre en pérdida, o el anexo trae la cifra del trimestre y no la acumulada (Terpel 2025-2026).
3. Diciembre de los anexos contra la ganancia anual de Supersociedades / acciones: más de 25 % de
   diferencia (Supersociedades redondea a 5.000 millones, así que solo se miran ganancias de 50.000
   millones o más).
"""
from __future__ import annotations

import argparse

import polars as pl

from invest import storage
from invest.fundamentales import UTILIDAD_MINIMA_SS, _fechas_split
from invest.ingest.supersociedades import PRECISION


def saltos_vpa(f: pl.DataFrame, umbral: float = 1.4) -> pl.DataFrame:
    splits = _fechas_split()
    v = (f.filter((pl.col("concepto") == "valor_patrimonial_accion") & (pl.col("base") == "separado") & (pl.col("valor") > 0))
         .sort("emisor_id", "fecha_corte")
         .with_columns(pl.col("valor").shift(1).over("emisor_id").alias("anterior"),
                       pl.col("fecha_corte").shift(1).over("emisor_id").alias("corte_anterior")))
    v = v.filter(pl.col("anterior").is_not_null()).with_columns((pl.col("valor") / pl.col("anterior")).alias("cociente"))
    v = v.filter((pl.col("cociente") > umbral) | (pl.col("cociente") < 1 / umbral))
    filas = [r for r in v.iter_rows(named=True)
             if not any(r["corte_anterior"] <= d <= r["fecha_corte"] for d in splits.get(r["emisor_id"], []))]
    return pl.DataFrame(filas, schema=v.schema).select(
        pl.lit("vpa_salta").alias("revision"), "emisor_id", "corte_anterior", "anterior", "fecha_corte", "valor",
        "fuente", pl.col("cociente").round(2))


def caidas_acumulado(f: pl.DataFrame) -> pl.DataFrame:
    u = (f.filter((pl.col("concepto") == "utilidad_accion") & (pl.col("base") == "separado"))
         .sort("emisor_id", "fecha_corte")
         .with_columns(pl.col("fecha_corte").dt.year().alias("_anio")))
    u = u.with_columns(pl.col("valor").shift(1).over(["emisor_id", "_anio"]).alias("anterior"),
                       pl.col("fecha_corte").shift(1).over(["emisor_id", "_anio"]).alias("corte_anterior"))
    return (u.filter((pl.col("anterior") > 0) & (pl.col("valor") > 0) & (pl.col("valor") < 0.6 * pl.col("anterior")))
            .select(pl.lit("upa_acumulada_baja").alias("revision"), "emisor_id", "corte_anterior", "anterior",
                    "fecha_corte", "valor", "fuente", (pl.col("valor") / pl.col("anterior")).round(2).alias("cociente")))


def diciembre_contra_supersociedades(f: pl.DataFrame) -> pl.DataFrame:
    ss = f.filter((pl.col("fuente") == "supersociedades") & (pl.col("concepto") == "utilidad_total")
                  & (pl.col("valor").abs() >= UTILIDAD_MINIMA_SS * PRECISION)).select("emisor_id", "fecha_corte", pl.col("valor").alias("_u"))
    acc = f.filter(pl.col("concepto") == "acciones_circulacion").select("emisor_id", pl.col("fecha_corte").alias("_fa"), pl.col("valor").alias("_acc"))
    d = (ss.join(acc, on="emisor_id").with_columns((pl.col("_fa") - pl.col("fecha_corte")).dt.total_days().abs().alias("_d"))
         .filter(pl.col("_d") <= 200).sort("_d").unique(["emisor_id", "fecha_corte"], keep="first")
         .with_columns((pl.col("_u") / pl.col("_acc")).alias("anterior")))
    upa = f.filter((pl.col("concepto") == "utilidad_accion") & (pl.col("base") == "separado")
                   & (pl.col("fuente") != "supersociedades_derivado")).select("emisor_id", "fecha_corte", "valor", "fuente")
    j = d.join(upa, on=["emisor_id", "fecha_corte"]).with_columns((pl.col("valor") / pl.col("anterior")).alias("cociente"))
    return (j.filter((pl.col("cociente") - 1).abs() > 0.25)
            .select(pl.lit("diciembre_vs_supersociedades").alias("revision"), "emisor_id",
                    pl.col("fecha_corte").alias("corte_anterior"), pl.col("anterior").round(2), "fecha_corte", "valor",
                    "fuente", pl.col("cociente").round(2)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="guardar la lista en este CSV")
    args = ap.parse_args()
    f = storage.leer_tabla("gold", "fact_fundamental")
    if f is None or f.is_empty():
        raise SystemExit("No hay gold/fact_fundamental: corra primero uv run invest modelo")
    nombres = {}
    emisores = storage.leer_dim("dim_emisor")
    if emisores is not None and "nombre" in emisores.columns:
        nombres = dict(emisores.select("emisor_id", "nombre").iter_rows())
    partes = [saltos_vpa(f), caidas_acumulado(f), diciembre_contra_supersociedades(f)]
    todo = pl.concat([p.with_columns(pl.col("anterior").cast(pl.Float64), pl.col("valor").cast(pl.Float64)) for p in partes])
    todo = todo.with_columns(pl.col("emisor_id").replace_strict(nombres, default=pl.col("emisor_id")).alias("emisor"))
    with pl.Config(tbl_rows=200, tbl_width_chars=200, fmt_str_lengths=40):
        for nombre, p in zip(("Valor patrimonial que salta", "Utilidad acumulada que baja", "Diciembre contra Supersociedades"), partes):
            sub = todo.filter(pl.col("revision") == p.get_column("revision").first()) if not p.is_empty() else p
            print(f"\n{nombre}: {sub.height}")
            if not sub.is_empty():
                print(sub.select("emisor", "corte_anterior", "anterior", "fecha_corte", "valor", "fuente", "cociente"))
    if args.csv:
        todo.write_csv(args.csv)
        print(f"\nGuardado en {args.csv}")


if __name__ == "__main__":
    main()
