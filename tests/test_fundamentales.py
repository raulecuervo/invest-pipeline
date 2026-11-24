from datetime import date

import polars as pl

from invest import fundamentales

CORTE = date(2025, 9, 30)


def _celda(emisor, formato, fila, concepto, columna, valor, consolidado=False):
    return {"emisor_id": emisor, "fecha_corte": CORTE, "grupo": "trimestrales", "consolidado": consolidado,
            "formato": formato, "nombre_formato": "", "fila": fila, "concepto": concepto, "columna": columna,
            "valor_texto": str(valor), "valor": valor}


ANEXOS = pl.DataFrame([
    # Sector real (141): Grupo Sura
    _celda("CO-COT13", "141", 2, "Valor patrimonial x acción sin valorización", "VALOR", 41841.09),
    _celda("CO-COT13", "141", 3, "Utilidad por acción", "VALOR", 7510.89),
    _celda("CO-COT13", "141", 6, "Valor dividendo por accion ordinaria en efectivo", "VALOR", 1500.0),
    _celda("CO-COT13", "141", 13, "** TOTAL INGRESOS VTAS Y/O PRESTACION SERVICIOS **", "VALOR", 3.6741e12),
    # Campo sin diligenciar (Promigas) y emisor que reporta en dólares (BAC Holding)
    _celda("CO-COI04", "141", 2, "Valor patrimonial x acción sin valorización", "VALOR", 0.0),
    _celda("CO-PAT07", "141", 2, "Valor patrimonial x acción sin valorización", "VALOR", 0.07),
    # Entidad vigilada (211): Banco de Bogotá; el valor por acción viene en la otra columna
    _celda("CO-COB01", "211", 4, "TOTAL", "Número de acciones", 355251068.0),
    _celda("CO-COB01", "211", 4, "TOTAL", "Número de accionistas", 15855.0),
    _celda("CO-COB01", "211", 7, "TOTAL", "Número de acciones", 100.0),
    _celda("CO-COB01", "211", 24, "VALOR PATRIMONIAL POR ACCION", "Número de acciones", 0.0),
    _celda("CO-COB01", "211", 24, "VALOR PATRIMONIAL POR ACCION", "Número de accionistas", 47021.41),
    _celda("CO-COB01", "211", 25, "UTILIDAD POR ACCION", "Número de accionistas", 2880.66),
    _celda("CO-COB01", "211", 26, "PERDIDA POR ACCION", "Número de accionistas", 0.0),
    # Los consolidados (formato 58) no son fundamentales
    _celda("CO-COB01", "58", 1, "Filial", "ACTIVOS TOTALES", 1.0, consolidado=True),
])


def _valor(df, emisor, concepto):
    return df.filter((pl.col("emisor_id") == emisor) & (pl.col("concepto") == concepto)).row(0, named=True)


def test_anexos_141_y_211():
    df = fundamentales.desde_anexos(ANEXOS)
    vp = _valor(df, "CO-COT13", "valor_patrimonial_accion")
    assert vp["valor"] == 41841.09 and vp["base"] == "separado" and vp["fuente"] == "anexo_141"
    assert vp["fecha_disponible"] == date(2025, 11, 29)  # corte + 60 días
    assert _valor(df, "CO-COT13", "ingresos_separados")["valor"] == 3.6741e12
    assert _valor(df, "CO-COB01", "valor_patrimonial_accion")["valor"] == 47021.41
    assert _valor(df, "CO-COB01", "utilidad_accion")["valor"] == 2880.66
    acciones = _valor(df, "CO-COB01", "acciones_circulacion")
    assert acciones["valor"] == 355251068.0 and acciones["unidad"] == "acciones"
    assert df.filter(pl.col("concepto") == "perdida_accion").is_empty()
    assert df.filter(pl.col("emisor_id") == "CO-COI04").is_empty()
    assert _valor(df, "CO-PAT07", "valor_patrimonial_accion")["unidad"] == "usd_por_accion"


def test_xbrl_solo_periodo_del_informe_y_en_pesos():
    base = {"emisor_id": "CO-COE15", "nit": "", "entidad_sfc": "260-34", "fecha_corte": CORTE,
            "tipo_informe": "consolidado", "radicado": "1", "fecha_transmision": date(2025, 11, 12),
            "fuente_id": "sfc_rnve_niif", "ingest_ts": None}
    xbrl = pl.DataFrame([
        {**base, "concepto": "ifrs:Assets", "tipo_periodo": "instante", "fecha_inicio": CORTE, "fecha_fin": CORTE,
         "valor": 77487000000.0, "unidad": "COP", "escala": "miles_de_pesos"},
        {**base, "concepto": "ifrs:Assets", "tipo_periodo": "instante", "fecha_inicio": date(2024, 12, 31),
         "fecha_fin": date(2024, 12, 31), "valor": 1.0, "unidad": "COP", "escala": "miles_de_pesos"},
        {**base, "concepto": "ifrs:ProfitLossAttributableToOwnersOfParent", "tipo_periodo": "duracion",
         "fecha_inicio": date(2025, 7, 1), "fecha_fin": CORTE, "valor": 5.0, "unidad": "COP", "escala": "miles_de_pesos"},
        {**base, "concepto": "ifrs:ProfitLossAttributableToOwnersOfParent", "tipo_periodo": "duracion",
         "fecha_inicio": date(2025, 1, 1), "fecha_fin": CORTE, "valor": 1849600000.0, "unidad": "COP",
         "escala": "miles_de_pesos"},
        {**base, "concepto": "ifrs:BasicEarningsLossPerShare", "tipo_periodo": "duracion",
         "fecha_inicio": date(2025, 1, 1), "fecha_fin": CORTE, "valor": 1669.82, "unidad": "peso", "escala": "pesos"},
    ])
    df = fundamentales.desde_xbrl(xbrl)
    assert df.height == 3  # sin el comparativo ni el trimestre suelto
    assert _valor(df, "CO-COE15", "activos")["valor"] == 77487000000.0 * 1000
    assert _valor(df, "CO-COE15", "utilidad_controladora")["valor"] == 1849600000.0 * 1000
    upa = _valor(df, "CO-COE15", "upa_basica")
    assert upa["valor"] == 1669.82 and upa["unidad"] == "pesos_por_accion"
    assert upa["fecha_disponible"] == date(2025, 11, 12) and upa["base"] == "consolidado"


def test_xbrl_de_emisores_en_dolares():
    # BAC: montos en dólares sin escalar y un corte guardado como pesos; Canacol: miles de dólares
    def hecho(emisor, corte, valor, escala):
        return {"emisor_id": emisor, "nit": "", "entidad_sfc": "", "fecha_corte": corte, "tipo_informe": "consolidado",
                "concepto": "ifrs:Assets", "tipo_periodo": "instante", "fecha_inicio": corte, "fecha_fin": corte,
                "valor": valor, "unidad": "COP", "escala": escala, "radicado": "1", "fecha_transmision": corte,
                "fuente_id": "sfc_rnve_niif", "ingest_ts": None}
    xbrl = pl.DataFrame([
        hecho("CO-PAT07", date(2025, 12, 31), 41932672874.0, "miles_de_dolares"),
        hecho("CO-PAT07", date(2026, 6, 30), 48130000000.0, "miles_de_pesos"),
        hecho("CO-CAC13", date(2024, 12, 31), 1215777.0, "miles_de_dolares"),
    ])
    df = fundamentales.desde_xbrl(xbrl).sort(["emisor_id", "fecha_corte"])
    bac = df.filter(pl.col("emisor_id") == "CO-PAT07")
    assert bac.get_column("valor").to_list() == [41932672874.0, 48130000000.0]
    assert set(bac.get_column("unidad")) == {"usd"}
    canacol = _valor(df, "CO-CAC13", "activos")
    assert canacol["valor"] == 1215777.0 * 1000 and canacol["unidad"] == "usd"
