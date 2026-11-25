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
                "tecnico_valido": k != 0, "volatilidad_anual": 0.2 + 0.01 * k, "drawdown_maximo": None, "beta": None,
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


def test_global_necesita_un_minimo_de_factores():
    # Solo valor y momentum (sin riesgo): con el mínimo de 3 factores el global queda vacío
    p = _panel().with_columns(pl.lit(None, pl.Float64).alias("volatilidad_anual"))
    _, matriz = puntajes.calcular(p, validados=set())
    assert matriz.get_column("puntaje_global").is_null().all()
    assert matriz.get_column("puntaje_valor").is_not_null().all()


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


def test_distancia_sma200_no_cuenta_si_la_accion_no_es_liquida(carpeta_datos):
    """El momentum de una acción poco líquida no usa la media de 200 negocios (abarca años)."""
    from invest import storage
    fecha = date(2026, 11, 23)
    especies = ["coa01paao001", "coa02paao002"]
    (carpeta_datos / "gold" / "dims").mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"especie": especies, "nombre": ["A", "B"], "sector_sfc": ["X", "X"], "clase_accion": ["ordinaria"] * 2,
                  "emisor_id": ["CO-COA01", "CO-COA02"], "activo_id": ["A", "B"]}).write_parquet(
        carpeta_datos / "gold" / "dims" / "dim_activo.parquet")
    storage.escribir_completa("gold", "fact_valoracion", pl.DataFrame({
        "fecha": [fecha, fecha], "especie": especies, "emisor_id": ["CO-COA01", "CO-COA02"], "p_vl": [1.0, 1.0]}))
    storage.escribir_por_anio("gold", "fact_tecnico_diario", pl.DataFrame({
        "fecha": [fecha, fecha], "especie": especies, "distancia_sma200": [4.18, 0.05], "tendencia_alcista": [True, True],
        "caida_desde_max_52s": [0.0, -0.1], "rsi_14": [84.0, 50.0], "tecnico_valido": [False, True]}), "fecha")
    p = puntajes.panel().sort("especie")
    assert p.get_column("distancia_sma200").to_list() == [None, 0.05]


def test_metrica_con_pocos_pares_en_el_sector_se_compara_con_el_universo():
    """El sector X tiene 9 acciones en el universo, pero solo 2 con crecimiento: esa métrica no se puede
    comparar dentro del sector (con 2 datos la mediana y la dispersión no dicen nada) y va contra el universo
    (que aquí es el mismo conjunto, así que el grupo cambia a "universo")."""
    p = _panel().with_columns(
        pl.when(pl.col("especie").is_in(["e1", "e2"])).then(pl.col("p_vl")).otherwise(None).alias("crecimiento_upa_12m"))
    puntaje, _ = puntajes.calcular(p, validados=set())
    fila = puntaje.filter((pl.col("fecha") == date(2025, 1, 31)) & (pl.col("especie") == "e1")
                          & (pl.col("elemento") == "crecimiento_upa_12m")).row(0, named=True)
    assert fila["grupo_comparacion"] == "universo" and fila["pares"] == 2
    # El valor (p_vl) sigue comparándose dentro del sector
    v = puntaje.filter((pl.col("fecha") == date(2025, 1, 31)) & (pl.col("especie") == "e1")
                       & (pl.col("elemento") == "p_vl")).row(0, named=True)
    assert v["grupo_comparacion"] == "X"
