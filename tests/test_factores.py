import copy
import math
import random
from datetime import date

import polars as pl

from invest import factores, storage


def test_newey_west_y_holm():
    m, t, p = factores.newey_west([0.1] * 5 + [0.12] * 5, 2)
    assert abs(m - 0.11) < 1e-12 and t > 5 and p < 1e-3
    adj = factores.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adj == {"a": 0.03, "b": 0.06, "c": 0.06}


def test_fines_de_mes_usa_sesiones_xbog():
    meses = factores.fines_de_mes(date(2024, 1, 1), date(2024, 3, 31))
    assert meses == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 27)]  # 28 y 29 de marzo: Semana Santa


def _panel_sintetico(semilla: int = 7) -> pl.DataFrame:
    """Factores ya calculados: 'bueno' anticipa el retorno siguiente, 'ruido' no."""
    rnd = random.Random(semilla)
    filas = []
    meses = factores.fines_de_mes(date(2008, 1, 1), date(2025, 12, 31))
    for m in meses:
        for k in range(20):
            senal = rnd.gauss(0, 1)
            ret = 0.01 * senal + rnd.gauss(0, 0.03)
            filas += [{"fecha": m, "especie": f"e{k}", "retorno_siguiente": ret, "factor": "bueno", "valor": senal},
                      {"fecha": m, "especie": f"e{k}", "retorno_siguiente": ret, "factor": "ruido", "valor": rnd.gauss(0, 1)}]
    return pl.DataFrame(filas)


def test_validacion_distingue_senal_de_ruido(monkeypatch):
    cfg = copy.deepcopy(factores.config())
    cfg["factores"] = {"bueno": {"signo": 1}, "ruido": {"signo": 1}}
    monkeypatch.setattr(factores, "config", lambda: cfg)
    monkeypatch.setattr(storage, "escribir_completa", lambda *a, **k: None)
    tabla, resumen = factores.validar(_panel_sintetico())
    v = dict(tabla.select("factor", "veredicto").iter_rows())
    assert v["bueno"] == "validado" and v["ruido"] in ("no_validado", "debil")
    bueno = tabla.filter(pl.col("factor") == "bueno").row(0, named=True)
    assert bueno["ic_medio"] > 0.2 and bueno["spread_mensual"] > 0


def test_construir_sin_mirar_al_futuro(carpeta_datos):
    # Dos años de una acción líquida con retorno diario constante, y un salto atípico que no debe contar
    sesiones = [d for d in factores.sesiones_entre(date(2020, 1, 1), date(2021, 12, 31))]
    filas, liq = [], []
    for e, r in (("a", 0.001), ("b", -0.0005)):
        for i, d in enumerate(sesiones):
            atip = e == "a" and d == date(2021, 6, 1)
            filas.append({"fecha": d, "especie": e, "precio_promedio": 100.0, "dividendo": None,
                          "retorno_total": 0.9 if atip else r, "salto_atipico": atip})
            liq.append({"fecha": d, "especie": e, "monto_medio_60": 1e9, "liquida": True})
    storage.escribir_completa("gold", "fact_retorno_diario", pl.DataFrame(filas))
    storage.escribir_completa("gold", "fact_liquidez_diaria", pl.DataFrame(liq))
    tabla, resumen = factores.construir()
    mom = tabla.filter((pl.col("factor") == "momentum_12_1") & (pl.col("especie") == "a")
                       & (pl.col("fecha") == date(2021, 7, 30))).row(0, named=True)
    # 12-1 meses: unos 230 días a 0,1 % (el salto del 1-jun no cuenta)
    assert 0.2 < mom["valor"] < 0.3
    vol = tabla.filter((pl.col("factor") == "volatilidad_1a") & (pl.col("especie") == "a")).get_column("valor")
    assert vol.max() < 1e-9  # retornos constantes y el salto excluido: volatilidad cero
    # El retorno siguiente de julio es el de agosto (índice limpio)
    assert math.isclose(mom["retorno_siguiente"], 1.001 ** len(factores.sesiones_entre(date(2021, 8, 2), date(2021, 8, 31))) - 1,
                        rel_tol=1e-9)
