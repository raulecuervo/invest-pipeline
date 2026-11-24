from datetime import date

import polars as pl

from invest import puntajes


def _panel():
    filas = []
    for fecha in (date(2025, 1, 31), date(2025, 2, 28)):
        for k in range(10):
            filas.append({
                "fecha": fecha, "especie": f"e{k}", "emisor_id": f"E{k}", "nombre": f"E{k}", "sector_sfc": "X",
                "clase_accion": "ordinaria", "en_universo": k < 9, "precio": 100.0, "capitalizacion": 1e9,
                "capitalizacion_usd": 1e6, "p_vl": 0.5 + 0.1 * k, "p_u": None, "rendimiento_utilidad": None,
                "rendimiento_dividendo_12m": 0.05, "roe_12m": None, "apalancamiento": None,
                "crecimiento_upa_12m": None, "crecimiento_ingresos_12m": None,
                "momentum_12_1": 0.1 * k if fecha.month == 1 else -0.1 * k, "momentum_12_1_percentil": k / 10,
                "distancia_sma200": None, "tendencia_alcista": True, "caida_desde_max_52s": -0.1, "rsi_14": 50.0,
                "tecnico_valido": k != 0, "volatilidad_anual": None, "drawdown_maximo": None, "beta": None,
                "percentil_validado": None, "tercio_validado": None,
            })
    return pl.DataFrame(filas)


def test_escala_z_robusta_y_faltantes():
    puntaje, matriz = puntajes.calcular(_panel(), validados={"momentum_12_1"})
    enero = matriz.filter(pl.col("fecha") == date(2025, 1, 31)).sort("especie")
    # p_vl: menos es mejor; la mediana del universo (e0..e8) es e4 → puntaje 5,5
    assert enero.filter(pl.col("especie") == "e4").get_column("puntaje_valor")[0] == 5.5
    assert enero.get_column("puntaje_valor").to_list() == sorted(enero.get_column("puntaje_valor").to_list(), reverse=True)
    assert enero.get_column("puntaje_valor").max() <= 10 and enero.get_column("puntaje_valor").min() >= 1
    # Sin métricas de calidad: el factor queda vacío (no 5) y el global se calcula con los demás
    assert enero.get_column("puntaje_calidad").is_null().all()
    assert enero.get_column("puntaje_global").is_not_null().all()
    # e9 está fuera del universo: recibe puntaje contra el universo, pero no define la mediana
    assert enero.filter(pl.col("especie") == "e9").get_column("puntaje_valor")[0] < 5.5
    # Técnico N/A si la acción no es líquida ese día
    assert enero.filter(pl.col("especie") == "e0").get_column("puntaje_tecnico")[0] is None
    # Matriz informativa: los factores de la matriz no están todos validados
    assert (matriz.get_column("estado") == "informativo").all() and matriz.get_column("decision").is_null().all()
    # Auditoría: cada métrica con su valor, z, percentil y pares
    fila = puntaje.filter((pl.col("fecha") == date(2025, 1, 31)) & (pl.col("especie") == "e4")
                          & (pl.col("elemento") == "p_vl")).row(0, named=True)
    assert fila["z"] == 0 and fila["pares"] == 9 and fila["grupo_comparacion"] == "X"  # 9 pares del sector: alcanza el mínimo de 8


def test_screener_marca_entradas():
    _, matriz = puntajes.calcular(_panel(), validados=set())
    matriz = matriz.with_columns(pl.lit(None, pl.Utf8).alias("tercio_validado"))
    c = puntajes.screeners(matriz)
    mom = c.filter(pl.col("screener_id") == "momentum")
    enero = set(mom.filter(pl.col("fecha") == date(2025, 1, 31)).get_column("especie"))
    febrero = mom.filter(pl.col("fecha") == date(2025, 2, 28))
    assert enero == {"e7", "e8"}  # percentil >= 2/3 y en el universo
    assert mom.filter(pl.col("fecha") == date(2025, 1, 31)).get_column("nueva").all()
    assert febrero.filter(pl.col("especie") == "e7").get_column("nueva").to_list() == [False]
