"""Motor de backtesting: simulación, métricas y series de los activos, contra cuentas hechas a mano."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import polars as pl
import pytest

from invest import backtest as bt


def test_rebalanceo_anual_a_mano():
    meses = bt.meses_entre(date(2020, 1, 31), date(2021, 1, 31))  # 13 fines de mes
    # A sube 10 % en febrero y luego queda quieta; B queda quieta
    a = np.array([100, 110] + [110] * 11, dtype=float)
    b = np.full(13, 50.0)
    p = bt.Parametros(pesos={"a": 0.5, "b": 0.5}, rebalanceo="anual", valor_inicial=1000, con_costos=False)
    r = bt.simular(np.column_stack([a, b]), meses, ["a", "b"], p, np.zeros(2))
    assert r.valor[1] == pytest.approx(1050)            # 550 + 500
    assert r.pesos[1][0] == pytest.approx(550 / 1050)
    assert r.rebalanceos == [date(2020, 12, 31)]        # solo en diciembre
    assert r.pesos[11] == pytest.approx([0.5, 0.5])
    assert r.indice[-1] == pytest.approx(105.0)
    assert r.rotacion == pytest.approx(50 / r.valor.mean() / 1.0)  # se vendieron 25 de A y se compraron 25 de B


def test_costos_reducen_el_indice():
    meses = bt.meses_entre(date(2020, 1, 31), date(2020, 3, 31))
    a = np.array([100, 120, 120], dtype=float)
    b = np.array([100, 100, 100], dtype=float)
    p = bt.Parametros(pesos={"a": 0.5, "b": 0.5}, rebalanceo="mensual", valor_inicial=1000)
    r = bt.simular(np.column_stack([a, b]), meses, ["a", "b"], p, np.array([0.01, 0.01]))
    # Compra inicial: 1 % de 1.000 = 10. Febrero: 495 × 1,2 = 594 y 495 → total 1.089; se mueven 49,5 de cada lado
    assert r.valor[0] == pytest.approx(990)
    total = 594 + 495
    mov = abs(total / 2 - 594) + abs(total / 2 - 495)
    costo = 0.01 * mov
    assert r.valor[1] == pytest.approx(total - costo, rel=1e-4)
    assert r.indice[1] == pytest.approx(100 * total / 990 * (total - costo) / total, rel=1e-4)
    assert r.costos > 0


def test_bandas_y_nunca():
    meses = bt.meses_entre(date(2020, 1, 31), date(2020, 6, 30))
    a = np.array([100, 104, 108, 125, 125, 125], dtype=float)
    b = np.full(6, 100.0)
    base = dict(pesos={"a": 0.5, "b": 0.5}, valor_inicial=1000, con_costos=False)
    r = bt.simular(np.column_stack([a, b]), meses, ["a", "b"], bt.Parametros(rebalanceo="bandas", banda=0.05, **base), np.zeros(2))
    assert r.rebalanceos == [date(2020, 4, 30)]  # 125/225 = 55,6 % pasa la banda de 5 puntos
    r = bt.simular(np.column_stack([a, b]), meses, ["a", "b"], bt.Parametros(rebalanceo="nunca", **base), np.zeros(2))
    assert r.rebalanceos == [] and r.pesos[-1][0] == pytest.approx(625 / 1125)


def test_aportes_no_son_rentabilidad():
    meses = bt.meses_entre(date(2020, 1, 31), date(2020, 12, 31))
    plano = np.full((12, 1), 1.0)
    p = bt.Parametros(pesos={"x": 1.0}, valor_inicial=1000, aporte_mensual=100, con_costos=False)
    r = bt.simular(plano, meses, ["x"], p, np.zeros(1))
    assert r.valor[-1] == pytest.approx(2100) and r.indice[-1] == pytest.approx(100.0) and r.aportes == 1100


def test_metricas_caida_y_recuperacion():
    meses = bt.meses_entre(date(2020, 1, 31), date(2021, 6, 30))
    indice = np.array([100, 110, 99, 88, 95, 105, 111] + [111 * 1.01 ** k for k in range(1, 12)])
    m = bt.metricas(indice, meses, efectivo=np.ones(18), inflacion=None)
    assert m["caida_maxima"] == pytest.approx(88 / 110 - 1)
    assert (m["caida_desde"], m["caida_hasta"]) == ("2020-02-29", "2020-04-30")
    assert m["meses_recuperacion"] == 5                 # de febrero (110) a julio (111)
    assert m["retorno_anual"] == pytest.approx((indice[-1] / 100) ** (12 / 17) - 1)
    r = indice[1:] / indice[:-1] - 1
    assert m["volatilidad_anual"] == pytest.approx(np.std(r, ddof=1) * math.sqrt(12))
    assert m["cvar_95_mensual"] == pytest.approx(r.min())   # 17 meses: el peor 5 % es un mes
    assert m["sharpe"] == pytest.approx(r.mean() * 12 / (np.std(r, ddof=1) * math.sqrt(12)))


def test_ventanas_reales():
    n = 12 * 6 + 1
    indice = 100 * 1.01 ** np.arange(n)
    inflacion = 100 * 1.005 ** np.arange(n)
    v = {w["anios"]: w for w in bt.ventanas(indice, inflacion)}
    assert set(v) == {3, 5}
    assert v[3]["ventanas"] == n - 36 and v[3]["peor"] == pytest.approx(1.01 ** 12 - 1)
    assert v[5]["mediana_real"] == pytest.approx((1.01 / 1.005) ** 12 - 1)


def test_nivel_tasa_crece_con_la_tasa_vigente():
    diaria = pl.DataFrame({"fecha": [date(2020, 1, 1), date(2020, 7, 1)], "valor": [10.0, 0.0]})
    meses = [date(2019, 12, 31), date(2020, 1, 31), date(2020, 12, 31)]
    n = bt.nivel_tasa(diaria, meses)
    assert math.isnan(n[0])
    assert n[1] == pytest.approx(1.10 ** (30 / 365))
    assert n[2] == pytest.approx(1.10 ** (182 / 365))   # desde julio la tasa es 0


def test_nivel_bono_a_la_par_y_con_subida_de_tasa():
    quieto = bt.nivel_bono(np.array([8.0, 8.0, 8.0]), 10)
    assert quieto[1] / quieto[0] - 1 == pytest.approx(0.08 / 12, rel=1e-3)   # tasa quieta: gana el cupón
    sube = bt.nivel_bono(np.array([8.0, 9.0]), 10)
    duracion = (1 - 1.09 ** -10) / 0.09
    assert sube[1] - 1 == pytest.approx(0.08 / 12 - duracion * 0.01, abs=0.004)   # pierde cerca de duración × 1 punto
    assert math.isnan(bt.nivel_bono(np.array([np.nan, 8.0]), 10)[0])


class DatosFalsos(bt.Datos):
    def __init__(self):
        super().__init__()
        dias = pl.date_range(date(2015, 1, 1), date(2020, 12, 31), "1d", eager=True)
        self._macro = pl.concat([
            pl.DataFrame({"fecha": dias, "serie_id": "banrep_15324", "valor": 5.0}),
            pl.DataFrame({"fecha": dias, "serie_id": "fred_dgs3mo", "valor": 2.0}),
            pl.DataFrame({"fecha": pl.date_range(date(2015, 1, 1), date(2020, 12, 1), "1mo", eager=True),
                          "serie_id": "ipc_co", "valor": 100.0}),
        ])
        # Una acción que sube 1 % al mes desde 2016 y deja de negociar en 2019
        meses = bt.meses_entre(date(2016, 1, 31), date(2020, 12, 31))
        self._tecnico = pl.DataFrame({"fecha": meses, "especie": "x1", "valor": [100 * 1.01 ** k for k in range(len(meses))]}) \
            .filter((pl.col("fecha") < date(2019, 3, 1)) | (pl.col("fecha") > date(2019, 8, 1)))
        self._indices = pl.DataFrame(schema={"fecha": pl.Date, "indice_id": pl.Utf8, "moneda": pl.Utf8, "valor": pl.Float64})
        self._trm = pl.DataFrame({"fecha": dias, "valor": 4000.0})


def test_correr_con_periodo_comun_y_avisos():
    r = bt.correr(bt.Parametros(pesos={"accion:x1": 0.5, "efectivo_cop": 0.5}, rebalanceo="nunca", con_costos=False,
                                hasta=date(2020, 12, 31)), DatosFalsos(), benchmark={"efectivo_cop": 1.0})
    assert r["periodo"]["desde"] == "2016-01-31" and r["periodo"]["hasta"] == "2020-12-31"
    assert any("4 meses seguidos sin negocios" in a for a in r["avisos"])
    assert r["metricas"]["retorno_real_anual"] == pytest.approx(r["metricas"]["retorno_anual"])  # IPC plano
    assert r["benchmark"]["metricas"]["retorno_anual"] == pytest.approx(0.05, abs=1e-3)
    assert r["serie"][0]["indice"] == 100 and "indice_benchmark" in r["serie"][-1]
    # En dólares el efectivo en pesos rinde lo mismo (TRM fija)
    usd = bt.correr(bt.Parametros(pesos={"efectivo_cop": 1.0}, moneda="USD", hasta=date(2020, 12, 31)), DatosFalsos())
    assert usd["metricas"]["retorno_anual"] == pytest.approx(0.05, abs=1e-3)


@pytest.mark.parametrize("pesos, mensaje", [({"efectivo_cop": 0.5}, "suman"), ({"efectivo_cop": 1.2, "x": -0.2}, "negativos"),
                                            ({}, "no tiene")])
def test_pesos_no_validos(pesos, mensaje):
    with pytest.raises(ValueError, match=mensaje):
        bt.Parametros(pesos=pesos).validar()


def test_pesos_de_texto():
    assert bt.pesos_de_texto("acciones_co_igual=60, tes_10a=40") == {"acciones_co_igual": 0.6, "tes_10a": 0.4}
    assert bt.pesos_de_texto("efectivo_cop=1") == {"efectivo_cop": 1.0}
