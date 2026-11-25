import math

import pytest

from invest import rebalanceo


def _posiciones():
    # 100 de valor: 30 efectivo, 70 en dos acciones (40 + 30)
    return [
        {"especie": "EFECTIVO", "clase_activo_id": "EFECTIVO", "valor_cop": 30.0, "precio": 1.0, "cantidad": 30},
        {"especie": "a", "clase_activo_id": "ACCION", "valor_cop": 40.0, "precio": 1.0, "cantidad": 40, "emisor": "A"},
        {"especie": "b", "clase_activo_id": "ACCION", "valor_cop": 30.0, "precio": 1.0, "cantidad": 30, "emisor": "B"},
    ]


def test_completo_lleva_cada_clase_a_su_objetivo():
    r = rebalanceo.calcular(_posiciones(), {"ACCION": 0.5, "EFECTIVO": 0.5})
    clases = {c["clase_activo_id"]: c for c in r["clases"]}
    assert math.isclose(clases["ACCION"]["monto"], -20) and math.isclose(clases["EFECTIVO"]["monto"], 20)
    assert math.isclose(clases["ACCION"]["peso_despues"], 0.5)
    # La venta se reparte en proporción al valor de cada posición: 40/70 y 30/70 de 20 = 11,43 y 8,57; en
    # acciones enteras 11 y 8, y la que sobra va a la que más perdió al redondear (b)
    ops = {o["especie"]: o for o in r["operaciones"]}
    assert ops["a"]["operacion"] == "reducir" and ops["a"]["cantidad"] == 11 and ops["b"]["cantidad"] == 9


def test_solo_aportes_no_vende_y_llena_lo_que_falta():
    # Objetivo 80 % acciones: con un aporte de 20 el total es 120; el efectivo (30 + 20 = 50) supera su meta
    # de 24 en 26, justo lo que les falta a las acciones (96 − 70)
    r = rebalanceo.calcular(_posiciones(), {"ACCION": 0.8, "EFECTIVO": 0.2}, aporte=20, modo="solo_aportes")
    assert all(o["operacion"] == "aumentar" for o in r["operaciones"])
    clases = {c["clase_activo_id"]: c for c in r["clases"]}
    assert math.isclose(clases["ACCION"]["monto"], 26) and math.isclose(clases["EFECTIVO"]["monto"], -6)
    assert math.isclose(r["valor_despues"], 120)
    # Con un objetivo de 50 % no hay que comprar nada: no se vende y el aporte queda en efectivo
    r = rebalanceo.calcular(_posiciones(), {"ACCION": 0.5, "EFECTIVO": 0.5}, aporte=20, modo="solo_aportes")
    assert r["operaciones"] == []
    # Un retiro mayor que el efectivo sale de las acciones
    r = rebalanceo.calcular(_posiciones(), {"ACCION": 0.7, "EFECTIVO": 0.3}, aporte=-40, modo="solo_aportes")
    assert math.isclose({c["clase_activo_id"]: c for c in r["clases"]}["ACCION"]["monto"], -10)


def test_clase_sin_posiciones_queda_por_asignar_y_comision():
    r = rebalanceo.calcular(_posiciones(), {"ACCION": 0.5, "RENTA_FIJA": 0.3, "EFECTIVO": 0.2}, comision=0.01)
    clases = {c["clase_activo_id"]: c for c in r["clases"]}
    assert math.isclose(clases["RENTA_FIJA"]["monto"], 30)
    assert any("RENTA_FIJA" in a for a in r["avisos"])
    assert math.isclose(r["costo_estimado"], 0.01 * sum(abs(o["monto"]) for o in r["operaciones"]))


def test_maximo_por_emisor_y_errores():
    r = rebalanceo.calcular(_posiciones(), {"ACCION": 0.7, "EFECTIVO": 0.3}, maximo_por_emisor=0.35)
    assert any("A quedaría en 40,0 %" in a for a in r["avisos"])
    with pytest.raises(ValueError):
        rebalanceo.calcular(_posiciones(), {"ACCION": 1.0}, aporte=-200)
    with pytest.raises(ValueError):
        rebalanceo.calcular(_posiciones(), {"ACCION": 1.0}, modo="otro")
