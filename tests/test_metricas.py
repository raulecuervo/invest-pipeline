from datetime import date

import polars as pl



def test_metricas_que_cruzan_una_escision_quedan_vacias():
    """Cementos Argos escindió el 14-jul-2025: la utilidad de 12 meses a dic-2025 trae la ganancia de la
    operación y el crecimiento dic-2025 frente a dic-2024 cruza el cambio de estructura."""
    from invest import metricas

    filas = [("upa_12m", date(2025, 12, 31), 2083.87), ("upa_12m", date(2026, 9, 30), 405.99),
             ("crecimiento_upa_12m", date(2025, 12, 31), 12.48), ("crecimiento_upa_12m", date(2027, 6, 30), 0.1),
             ("crecimiento_upa_12m", date(2027, 9, 30), 0.2), ("vpa", date(2025, 12, 31), 9000.0)]
    df = pl.DataFrame([{"emisor_id": "CO-COD38", "fecha_corte": f, "fecha_disponible": f, "base": "separado", "metrica": m,
                        "valor": v, "unidad": "x"} for m, f, v in filas], schema=metricas.ESQUEMA)
    ev = pl.DataFrame({"emisor_id": ["CO-COD38"], "fecha_evento": [date(2025, 7, 14)]})
    r = metricas.sin_eventos_de_estructura(df, ev)
    # Se conserva la fila (el as-of no debe recuperar un corte más viejo) pero sin valor
    assert r.height == df.height
    assert r.get_column("valor").to_list() == [None, 405.99, None, None, 0.2, 9000.0]


def test_sucesion_cuenta_como_evento_de_estructura():
    from invest import metricas

    ev = metricas.eventos_estructura()
    cibest = ev.filter(pl.col("emisor_id") == "CO-COTK7").get_column("fecha_evento").to_list()
    assert cibest == [date(2025, 5, 16)]
