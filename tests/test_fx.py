from datetime import date

from invest import fx


def test_cad_a_pesos_con_trm_y_cad_por_usd():
    trm = {date(2023, 3, 30): 4600.0, date(2023, 4, 3): 4650.0, date(2023, 4, 4): 4700.0}
    cad = {date(2023, 3, 30): 1.35, date(2023, 3, 31): 1.36}  # sin cotización el 3 y 4 de abril
    t = fx.tasas_a_cop("CAD", trm, cad)
    assert abs(t[date(2023, 3, 30)] - 4600 / 1.35) < 1e-9
    assert abs(t[date(2023, 4, 4)] - 4700 / 1.36) < 1e-9  # última de los 7 días anteriores
    assert fx.tasas_a_cop("USD", trm, cad) == trm
    assert fx.tasas_a_cop("EUR", trm, cad) == {}
