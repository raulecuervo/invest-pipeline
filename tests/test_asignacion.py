"""Asignación de activos: supuestos por componentes y los tres métodos."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

pytest.importorskip("skfolio", exc_type=ImportError)

from invest import asignacion as asg  # noqa: E402
from invest import backtest as bt  # noqa: E402
from invest import storage  # noqa: E402


def test_promedio_de_convergencia():
    # De 8 % a 2 % en 4 años y luego 2 % por 6 años: (4 × 5 + 6 × 2) / 10
    assert asg._promedio_convergencia(0.08, 0.02, 4) == pytest.approx(0.032)
    assert asg._promedio_convergencia(0.05, 0.05, 3) == pytest.approx(0.05)


class Datos(bt.Datos):
    """Quince años de historia: efectivo al 6 %, un bono con tasa que oscila y acciones con ruido."""

    def __init__(self):
        super().__init__()
        dias = pl.date_range(date(2008, 1, 1), date.today(), "1d", eager=True)
        meses = pl.date_range(date(2008, 1, 1), date.today(), "1mo", eager=True)
        rng = np.random.default_rng(3)
        self._macro = pl.concat([
            pl.DataFrame({"fecha": dias, "serie_id": "banrep_15324", "valor": 6.0}),
            pl.DataFrame({"fecha": dias, "serie_id": "fred_dgs3mo", "valor": 4.0}),
            pl.DataFrame({"fecha": dias, "serie_id": "fred_dgs10", "valor": 4.5 + np.sin(np.arange(len(dias)) / 200)}),
            pl.DataFrame({"fecha": meses, "serie_id": "fred_colirltlt01stm", "valor": 9.0 + np.sin(np.arange(len(meses)) / 7)}),
            pl.DataFrame({"fecha": meses, "serie_id": "ipc_co", "valor": 100 * 1.003 ** np.arange(len(meses))}),
        ])
        self._indices = pl.DataFrame({"fecha": meses, "indice_id": "universo_igual", "moneda": "COP",
                                      "valor": 100 * np.cumprod(1 + rng.normal(0.008, 0.05, len(meses)))})
        self._tecnico = pl.DataFrame(schema={"fecha": pl.Date, "especie": pl.Utf8, "valor": pl.Float64})
        self._trm = pl.DataFrame({"fecha": dias, "valor": 4000 * 1.0001 ** np.arange(len(dias))})


@pytest.fixture
def valoracion(carpeta_datos):
    """Dos acciones: dividendo 5 % y 4 %; P/U de hoy 8 y 10, medianas de 10 años más altas."""
    filas = []
    for k, f in enumerate(pl.date_range(date(2016, 1, 31), date(2026, 10, 31), "1mo", eager=True)):
        filas += [{"fecha": f, "especie": "a", "p_u": 12.0, "rendimiento_dividendo_12m": 0.05},
                  {"fecha": f, "especie": "b", "p_u": 12.0, "rendimiento_dividendo_12m": 0.04}]
    filas += [{"fecha": date(2026, 11, 25), "especie": "a", "p_u": 8.0, "rendimiento_dividendo_12m": 0.05},
              {"fecha": date(2026, 11, 25), "especie": "b", "p_u": 10.0, "rendimiento_dividendo_12m": 0.04}]
    storage.escribir_por_anio("gold", "fact_valoracion", pl.DataFrame(filas), "fecha")
    return carpeta_datos


def test_supuestos_por_componentes(valoracion, monkeypatch):
    monkeypatch.setattr(storage, "leer_tabla", _con_ficha(storage.leer_tabla))
    s = asg.supuestos(["efectivo_cop", "tes_10a", "acciones_co_igual", "tesoro_10a"], Datos())
    cfg = asg.config()
    pi = cfg["inflacion_esperada"]["COP"]
    ahora = 1.06 / (1 + pi) - 1
    assert s["efectivo_cop"]["real"] == pytest.approx(asg._promedio_convergencia(ahora, cfg["tasa_real_neutral_cop"], 3))
    tasa_tes = Datos().macro("fred_colirltlt01stm").sort("fecha").get_column("valor")[-1] / 100
    assert s["tes_10a"]["real"] == pytest.approx((1 + tasa_tes) / (1 + pi) - 1)
    # Acciones: dividendo mediano 4,5 % + 1,5 % + (12 / 9) ^ (0,5 / 10) − 1
    assert s["acciones_co_igual"]["real"] == pytest.approx(0.045 + 0.015 + (12 / 9) ** 0.05 - 1)
    assert s["acciones_co_igual"]["bajo"] < s["acciones_co_igual"]["real"] < s["acciones_co_igual"]["alto"]
    ajustado = asg.supuestos(["efectivo_cop"], Datos(), {"efectivo_cop": 0.01})["efectivo_cop"]
    assert ajustado["real"] == 0.01 and ajustado["ajustado"]


def _con_ficha(original):
    def leer(capa, tabla):
        if (capa, tabla) == ("gold", "fact_ficha_accion"):
            return pl.DataFrame({"especie": ["a", "b"], "liquida": [True, True]})
        return original(capa, tabla)
    return leer


def test_tres_metodos(valoracion, monkeypatch):
    monkeypatch.setattr(storage, "leer_tabla", _con_ficha(storage.leer_tabla))
    r = asg.calcular(activos=["efectivo_cop", "tes_10a", "acciones_co_igual"], volatilidad_objetivo=0.08,
                     maximo_por_activo=0.7, datos=Datos())
    for nombre in asg.METODOS:
        m = r["metodos"][nombre]
        assert sum(m["pesos"].values()) == pytest.approx(1.0, abs=1e-6), nombre
        assert m["volatilidad"] > 0 and m["cvar_95_mensual"] < m["retorno_real"]
        assert sum(m["contribucion_riesgo"].values()) == pytest.approx(1.0, abs=1e-6)
    opt = r["metodos"]["optimizada"]
    assert max(opt["pesos"].values()) <= 0.7 + 1e-6
    assert opt["volatilidad"] <= 0.08 * 1.02  # la covarianza de la restricción y la del informe son parecidas
    # Benchmark por objetivo (crecimiento): incluye dólares aunque no esté en la lista de activos
    assert "efectivo_usd" in r["metodos"]["benchmark"]["pesos"]
    # Una vista pesimista con mucha confianza baja el peso de las acciones
    con_vista = asg.calcular(activos=["efectivo_cop", "tes_10a", "acciones_co_igual"], volatilidad_objetivo=0.08,
                             maximo_por_activo=0.7, vistas=[{"activo": "acciones_co_igual", "retorno_real": -0.02, "confianza": 0.9}],
                             datos=Datos())
    assert con_vista["metodos"]["optimizada"]["pesos"].get("acciones_co_igual", 0) < opt["pesos"].get("acciones_co_igual", 0)


def test_benchmark_con_la_asignacion_del_perfil():
    pesos, nota = asg._benchmark({"objetivo": "renta"}, [{"clase_activo_id": "ACCION", "peso_objetivo": 0.3},
                                                         {"clase_activo_id": "TES_TF", "peso_objetivo": 0.5},
                                                         {"clase_activo_id": "CDT", "peso_objetivo": 0.2}], [])
    assert pesos == pytest.approx({"acciones_co_igual": 0.3, "tes_10a": 0.5, "efectivo_cop": 0.2}) and "perfil" in nota
    pesos, nota = asg._benchmark({"objetivo": "preservacion"}, [], [])
    assert pesos["efectivo_cop"] == 0.5 and "preservacion" in nota


def test_volatilidad_imposible_usa_la_minima(valoracion, monkeypatch):
    monkeypatch.setattr(storage, "leer_tabla", _con_ficha(storage.leer_tabla))
    r = asg.calcular(activos=["tes_10a", "acciones_co_igual"], volatilidad_objetivo=0.01, maximo_por_activo=1.0, datos=Datos())
    assert "mínima volatilidad" in r["metodos"]["optimizada"]["nota"]
