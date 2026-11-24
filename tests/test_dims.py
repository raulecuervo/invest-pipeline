from datetime import date

import polars as pl

from invest import dims


def test_construir_dimensiones(carpeta_datos):
    rutas = dims.construir(desde=date(2025, 1, 1), hasta=date(2026, 12, 31))
    assert set(rutas) >= {"dim_fecha", "dim_sesion", "dim_pais", "dim_moneda", "dim_clase_activo", "dim_fuente"}
    pais = pl.read_parquet(rutas["dim_pais"])
    assert pais.filter(pl.col("pais_id") == "CO").get_column("mic")[0] == "XBOG"
    sesion = pl.read_parquet(rutas["dim_sesion"])
    festivo = sesion.filter((pl.col("fecha") == date(2026, 11, 16)) & (pl.col("mic") == "XBOG"))
    assert festivo.get_column("es_habil")[0] is False
    fuentes = pl.read_parquet(rutas["dim_fuente"])
    assert "trm_datos_gov" in fuentes.get_column("fuente_id").to_list()
