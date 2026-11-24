from datetime import date

import polars as pl

from invest import fundamentales, metricas, storage
from invest.ingest import supersociedades as ss

# Filas reales del conjunto 6cat-2gcs (formato de la API, nov-2026)
REGISTROS = [
    {"nit": "899999068.00", "raz_n_social": "ECOPETROL S.A.", "supervisor": "SUPERFINANCIERA", "ciiu": "610",
     "macrosector": "MINERO", "ingresos_operacionales": "$ 100.59", "ganancia_p_rdida": "$ 9.03",
     "total_activos": "$ 198.11", "total_pasivos": "$ 114.35", "total_patrimonio": "$ 83.76", "a_o_de_corte": "2025.00"},
    {"nit": "890922447.00", "raz_n_social": "CONSTRUCCIONES EL CONDOR S.A.", "supervisor": "SUPERFINANCIERA",
     "ciiu": "4210", "macrosector": "CONSTRUCCIÓN", "ingresos_operacionales": "$ 0.84", "ganancia_p_rdida": "-$ 0.27",
     "total_activos": "$ 1.40", "total_pasivos": "$ 1.06", "total_patrimonio": "$ 0.34", "a_o_de_corte": "2025.00"},
    # Sin cuadre (dato inventado para la prueba)
    {"nit": "899999068.00", "raz_n_social": "ECOPETROL S.A.", "supervisor": "SUPERFINANCIERA", "ciiu": "610",
     "macrosector": "MINERO", "ingresos_operacionales": "$1.00", "ganancia_p_rdida": "$0.10",
     "total_activos": "$10.00", "total_pasivos": "$2.00", "total_patrimonio": "$5.00", "a_o_de_corte": "2021.00"},
]


def test_cifras_en_billones():
    assert ss.billones("$ 198.11") == 198.11e12
    assert ss.billones("-$ 0.27") == -0.27e12
    assert ss.billones("") is None
    assert ss.nit_base("899999068.00") == "899999068"


def test_interpreta_y_valida(carpeta_datos):
    con = ss.ConectorSupersociedades()
    df = con.interpretar(REGISTROS)
    validas, cuarentena = con.validar(df)
    eco = validas.filter((pl.col("emisor_id") == "CO-COC04") & (pl.col("anio") == 2025)).row(0, named=True)
    assert eco["patrimonio"] == 83.76e12 and eco["ganancia"] == 9.03e12 and eco["fecha_corte"] == date(2025, 12, 31)
    assert validas.filter(pl.col("emisor_id") == "CO-COF20").get_column("ganancia")[0] == -0.27e12
    assert cuarentena.get_column("motivo").to_list() == ["no cuadra activo = pasivo + patrimonio"]


def _fund_anexos(emisor, vpa, acciones, upa):
    corte = date(2025, 12, 31)
    filas = [("valor_patrimonial_accion", vpa, "pesos_por_accion", "anexo_141"),
             ("acciones_circulacion", acciones, "acciones", "anexo_211"),
             ("utilidad_accion", upa, "pesos_por_accion", "anexo_141")]
    return pl.DataFrame([{"emisor_id": emisor, "fecha_corte": corte, "fecha_disponible": date(2026, 4, 30),
                          "concepto": c, "valor": v, "unidad": u, "base": "separado", "fuente": f}
                         for c, v, u, f in filas], schema=fundamentales.ESQUEMA)


def test_respaldo_anual_y_cotejo(carpeta_datos):
    con = ss.ConectorSupersociedades()
    validas, _ = con.validar(con.interpretar(REGISTROS[:2]))
    respaldo = fundamentales.desde_supersociedades(validas)
    assert set(respaldo.get_column("base")) == {"separado_anual"}
    assert respaldo.get_column("fecha_disponible").unique().to_list() == [date(2026, 6, 30)]
    # Ecopetrol: 41.116 millones de acciones × 2.037,1 de valor patrimonial ≈ 83,76 billones (cuadra);
    # utilidad por acción 219,6 × acciones ≈ 9,03 billones
    anexos = _fund_anexos("CO-COC04", 2037.1, 41_116_694_690.0, 219.6)
    c = fundamentales.cotejo(pl.concat([anexos, respaldo]))
    eco = {r["concepto"]: r for r in c.filter(pl.col("emisor_id") == "CO-COC04").iter_rows(named=True)}
    assert eco["patrimonio"]["estado"] == "ok" and eco["utilidad"]["estado"] == "ok"
    # Si el valor por acción y las acciones no dan el patrimonio, queda para revisar
    malo = fundamentales.cotejo(pl.concat([_fund_anexos("CO-COC04", 1800.0, 41_116_694_690.0, 219.6), respaldo]))
    fila = malo.filter((pl.col("emisor_id") == "CO-COC04") & (pl.col("concepto") == "patrimonio")).row(0, named=True)
    assert fila["estado"] == "revisar" and "no dan el patrimonio" in fila["nota"]
    # El Cóndor no tiene anexos en esta prueba: sin par
    assert set(c.filter(pl.col("emisor_id") == "CO-COF20").get_column("estado")) == {"sin_par"}


def test_metricas_de_respaldo(carpeta_datos):
    con = ss.ConectorSupersociedades()
    validas, _ = con.validar(con.interpretar(REGISTROS[:2]))
    m = metricas.fact_metrica_fundamental(fundamentales.desde_supersociedades(validas))
    condor = {r["metrica"]: r["valor"] for r in m.filter(pl.col("emisor_id") == "CO-COF20").iter_rows(named=True)}
    assert abs(condor["apalancamiento"] - 1.06 / 1.40) < 1e-9
    assert abs(condor["roe_anual"] - (-0.27 / 0.34)) < 1e-9
    assert set(m.get_column("base")) == {"separado_anual"}


def test_anexo_con_acciones_imposibles_o_utilidad_vacia():
    # El Cóndor dic-2025: 1.367 acciones en el 211 (celda mal diligenciada) → no se usa
    # Terpel dic-2025: utilidad por acción 0 en el 211 y 709,07 en el 141 → se usa la del 141
    def celda(emisor, formato, fila, concepto, columna, valor):
        return {"emisor_id": emisor, "fecha_corte": date(2025, 12, 31), "consolidado": False, "grupo": "trimestrales", "formato": formato,
                "fila": fila, "concepto": concepto, "columna": columna, "valor": valor}
    anexos = pl.DataFrame([
        celda("CO-COF20", "211", 5, "TOTAL", "NÚMERO DE ACCIONES", 1367.0),
        celda("CO-COG20", "211", 1, "UTILIDAD POR ACCION", "VALOR", 0.0),
        celda("CO-COG20", "141", 1, "UTILIDAD POR ACCION", "VALOR", 709.07),
    ])
    f = fundamentales.desde_anexos(anexos)
    assert f.filter(pl.col("concepto") == "acciones_circulacion").is_empty()
    upa = f.filter((pl.col("emisor_id") == "CO-COG20") & (pl.col("concepto") == "utilidad_accion"))
    assert upa.get_column("valor").to_list() == [709.07]


def test_construir_escribe_cotejo(carpeta_datos):
    con = ss.ConectorSupersociedades()
    validas, _ = con.validar(con.interpretar(REGISTROS[:2]))
    storage.escribir_completa("silver", "fact_ee_anual_supersociedades", validas)
    df, resumen = fundamentales.construir()
    assert resumen["con_respaldo_anual"] == 2
    assert storage.leer_tabla("gold", "fact_cotejo_fundamental").height > 0
