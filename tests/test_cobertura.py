from datetime import date

from invest import cobertura


def test_ultimo_corte_vencido():
    assert cobertura._ultimo_corte_vencido(date(2026, 11, 24)) == date(2026, 9, 30)
    assert cobertura._ultimo_corte_vencido(date(2026, 3, 1)) == date(2025, 9, 30)  # diciembre aún en plazo


def test_tabla_sin_datos(carpeta_datos):
    t = cobertura.tabla(date(2026, 11, 24))
    assert t.height >= 30 and "fuente_alterna" in t.columns
    assert (t.get_column("niif_sin_revisar") > 0).all()
