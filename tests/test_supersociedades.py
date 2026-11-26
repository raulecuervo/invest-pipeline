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


def test_vpa_con_valorizacion_y_anexo_descartado(monkeypatch):
    # Grupo Bolívar: el "sin valorización" del 141 es 13 % menor que el 211; manda el "con"
    # Grupo Cibest: su utilidad por acción del 141 está descartada en config/convenciones.yaml
    def celda(emisor, fila, concepto, valor, corte=date(2026, 3, 31)):
        return {"emisor_id": emisor, "fecha_corte": corte, "consolidado": False, "grupo": "trimestrales", "formato": "141",
                "fila": fila, "concepto": concepto, "columna": "VALOR", "valor": valor}
    anexos = pl.DataFrame([
        celda("CO-COT23", 1, "Valor patrimonial x acción con valorización", 175026.83),
        celda("CO-COT23", 2, "Valor patrimonial x acción sin valorización", 152583.0),
        celda("CO-COT13", 2, "Valor patrimonial x acción sin valorización", 42000.0),
        celda("CO-COTK7", 3, "Utilidad por acción", 3373.15),
        celda("CO-COTK7", 1, "Valor patrimonial x acción con valorización", 41000.0),
    ])
    f = fundamentales.desde_anexos(anexos)
    vpa = dict(f.filter(pl.col("concepto") == "valor_patrimonial_accion").select("emisor_id", "valor").iter_rows())
    assert vpa == {"CO-COT23": 175026.83, "CO-COT13": 42000.0, "CO-COTK7": 41000.0}
    assert f.filter(pl.col("concepto") == "utilidad_accion").is_empty()
    assert "vpa_con_valorizacion" not in f.get_column("concepto").to_list()


def test_construir_escribe_cotejo(carpeta_datos):
    con = ss.ConectorSupersociedades()
    validas, _ = con.validar(con.interpretar(REGISTROS[:2]))
    storage.escribir_completa("silver", "fact_ee_anual_supersociedades", validas)
    df, resumen = fundamentales.construir()
    assert resumen["con_respaldo_anual"] == 2
    assert storage.leer_tabla("gold", "fact_cotejo_fundamental").height > 0


def test_valor_por_accion_de_dos_anexos_manda_el_211():
    # Grupo Bolívar dic-2025: 155.896,7 en el 141 y 178.331 en el 211 (el que cuadra con Supersociedades)
    def celda(formato, fila, concepto, columna, valor):
        return {"emisor_id": "CO-COT23", "fecha_corte": date(2025, 12, 31), "consolidado": False, "grupo": "trimestrales",
                "formato": formato, "fila": fila, "concepto": concepto, "columna": columna, "valor": valor}
    anexos = pl.DataFrame([celda("141", 1, "VALOR PATRIMONIAL POR ACCION", "VALOR", 155896.7),
                           celda("211", 1, "VALOR PATRIMONIAL POR ACCION", "VALOR", 178331.0)])
    f = fundamentales.desde_anexos(anexos).filter(pl.col("concepto") == "valor_patrimonial_accion")
    assert f.get_column("valor").to_list() == [178331.0] and f.get_column("fuente").to_list() == ["anexo_211"]


def test_cifra_por_accion_absurda_se_descarta():
    # BAC (BHI): 0,08 dólares de valor patrimonial por acción, y en mar-2026 el 141 dice 128,41
    def fila(corte, valor):
        return {"emisor_id": "CO-PAT07", "fecha_corte": corte, "fecha_disponible": corte, "concepto": "valor_patrimonial_accion",
                "valor": valor, "unidad": "pesos_por_accion", "base": "separado", "fuente": "anexo_141"}
    df = pl.DataFrame([fila(date(2025, 12, 31), 0.08), fila(date(2026, 3, 31), 128.41), fila(date(2026, 6, 30), 0.08),
                       fila(date(2026, 9, 30), 0.09)], schema=fundamentales.ESQUEMA)
    limpio = fundamentales.descartar_saltos(df)
    assert limpio.get_column("valor").to_list() == [0.08, 0.08, 0.09]
    # Un cambio de nivel que persiste (p. ej. una reagrupación) no se descarta: el corte siguiente lo confirma
    df2 = pl.DataFrame([fila(date(2025, 12, 31), 1.0), fila(date(2026, 3, 31), 500.0), fila(date(2026, 6, 30), 510.0)],
                       schema=fundamentales.ESQUEMA)
    assert fundamentales.descartar_saltos(df2).height == 3
    # Tramo de dos cortes en otra escala entre vecinos que coinciden (Paz del Río 2020-2021), aunque haya años sin dato
    df3 = pl.DataFrame([fila(date(2019, 12, 31), 20.61), fila(date(2020, 12, 31), 0.02), fila(date(2021, 12, 31), 0.02),
                        fila(date(2024, 12, 31), 19.24)], schema=fundamentales.ESQUEMA)
    assert fundamentales.descartar_saltos(df3).get_column("valor").to_list() == [20.61, 19.24]


def test_corte_aislado_que_duplica_el_valor_patrimonial_se_descarta():
    # Grupo Sura mar-2026: el 141 dice 87.310 entre 42.455 (dic-2025) y 42.453 (jun-2026)
    def fila(corte, valor):
        return {"emisor_id": "CO-COT13", "fecha_corte": corte, "fecha_disponible": corte, "concepto": "valor_patrimonial_accion",
                "valor": valor, "unidad": "pesos_por_accion", "base": "separado", "fuente": "anexo_141"}
    df = pl.DataFrame([fila(date(2025, 12, 31), 42455.0), fila(date(2026, 3, 31), 87310.0), fila(date(2026, 6, 30), 42453.0),
                       fila(date(2026, 9, 30), 40211.0)], schema=fundamentales.ESQUEMA)
    assert fundamentales.descartar_punto_aislado(df).get_column("valor").to_list() == [42455.0, 42453.0, 40211.0]
    # Un cambio de 15 % (Grupo Bolívar, 141 contra 211) o vecinos que no coinciden: se conserva
    df2 = pl.DataFrame([fila(date(2025, 12, 31), 178331.0), fila(date(2026, 3, 31), 152583.0), fila(date(2026, 6, 30), 181027.0),
                        fila(date(2026, 9, 30), 60000.0), fila(date(2026, 12, 31), 90000.0)], schema=fundamentales.ESQUEMA)
    assert fundamentales.descartar_punto_aislado(df2).height == 5



def test_tramo_corto_fuera_de_escala_se_descarta_y_un_cambio_de_nivel_no():
    # BVC mar y jun-2019: 825 y 4.640 entre 8.167 (dic-2018) y 8.156 (sep-2019)
    def fila(corte, valor):
        return {"emisor_id": "CO-COR01", "fecha_corte": corte, "fecha_disponible": corte, "concepto": "valor_patrimonial_accion",
                "valor": valor, "unidad": "pesos_por_accion", "base": "separado", "fuente": "anexo_211"}
    df = pl.DataFrame([fila(date(2018, 12, 31), 8166.73), fila(date(2019, 3, 31), 825.06), fila(date(2019, 6, 30), 4639.55),
                       fila(date(2019, 9, 30), 8156.0), fila(date(2019, 12, 31), 8430.0)], schema=fundamentales.ESQUEMA)
    assert fundamentales.descartar_punto_aislado(df).get_column("valor").to_list() == [8166.73, 8156.0, 8430.0]
    # Una escisión que parte el patrimonio a la mitad y no vuelve se conserva entera
    df2 = pl.DataFrame([fila(date(2021, 9, 30), 1000.0), fila(date(2021, 12, 31), 1020.0), fila(date(2022, 3, 31), 500.0),
                        fila(date(2022, 6, 30), 510.0), fila(date(2022, 9, 30), 515.0)], schema=fundamentales.ESQUEMA)
    assert fundamentales.descartar_punto_aislado(df2).height == 5


def test_tramo_largo_en_miles_se_descarta():
    # Paz del Río: 0,02 en seis cortes seguidos (jun-2020 a sep-2022) entre 19,95 y 21,24
    def fila(corte, valor):
        return {"emisor_id": "CO-COD12", "fecha_corte": corte, "fecha_disponible": corte, "concepto": "valor_patrimonial_accion",
                "valor": valor, "unidad": "pesos_por_accion", "base": "separado", "fuente": "anexo_211"}
    cortes = [date(2020, 3, 31), date(2020, 6, 30), date(2020, 12, 31), date(2021, 6, 30), date(2021, 12, 31),
              date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31)]
    valores = [19.95, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 21.24]
    df = pl.DataFrame([fila(c, v) for c, v in zip(cortes, valores)], schema=fundamentales.ESQUEMA)
    assert fundamentales.descartar_saltos(df).get_column("valor").to_list() == [19.95, 21.24]


def test_anexo_de_menor_prioridad_en_otra_escala_se_descarta():
    def fila(corte, valor, fuente):
        return {"emisor_id": "CO-PAT07", "fecha_corte": corte, "fecha_disponible": corte, "concepto": "valor_patrimonial_accion",
                "valor": valor, "unidad": "pesos_por_accion", "base": "separado", "fuente": fuente}
    df = pl.DataFrame([fila(date(2026, 6, 30), 0.08, "anexo_211"), fila(date(2025, 9, 30), 0.07, "anexo_141"),
                       fila(date(2026, 9, 30), 128.41, "anexo_141")], schema=fundamentales.ESQUEMA)
    assert sorted(fundamentales.descartar_fuente_discordante(df).get_column("valor").to_list()) == [0.07, 0.08]


def test_utilidad_por_accion_imposible_frente_al_valor_patrimonial():
    def fila(corte, concepto, valor):
        return {"emisor_id": "CO-PAT07", "fecha_corte": corte, "fecha_disponible": corte, "concepto": concepto,
                "valor": valor, "unidad": "pesos_por_accion", "base": "separado", "fuente": "anexo_141"}
    df = pl.DataFrame([fila(date(2025, 9, 30), "valor_patrimonial_accion", 0.07), fila(date(2025, 9, 30), "utilidad_accion", 53.67),
                       fila(date(2025, 12, 31), "utilidad_accion", 0.01), fila(date(2026, 9, 30), "utilidad_accion", -0.02)],
                      schema=fundamentales.ESQUEMA)
    limpio = fundamentales.descartar_upa_imposible(df)
    assert limpio.filter(pl.col("concepto") == "utilidad_accion").get_column("valor").to_list() == [0.01, -0.02]  # 2026-09 sin VPA cercano: no se controla


def test_ebitda_deuda_neta_y_ev(carpeta_datos):
    from invest import metricas

    def fila(corte, concepto, valor, emisor="CO-COE15"):
        return {"emisor_id": emisor, "fecha_corte": corte, "fecha_disponible": corte, "concepto": concepto, "valor": valor,
                "unidad": "pesos", "base": "consolidado", "fuente": "xbrl"}
    filas = []
    for emisor in ("CO-COE15", "CO-COT29"):  # ISA y Grupo Aval (financiero: excluido)
        for corte, uop, da in ((date(2024, 12, 31), 8.0, 1.0), (date(2025, 9, 30), 6.0, 0.75), (date(2025, 12, 31), 9.0, 1.2),
                               (date(2026, 9, 30), 7.0, 0.9)):
            filas += [fila(corte, "utilidad_operacional", uop, emisor), fila(corte, "depreciacion_amortizacion", da, emisor),
                      fila(corte, "ingresos_consolidados", 20.0, emisor)]
        filas += [fila(date(2026, 9, 30), "obligaciones_financieras_corrientes", 3.0, emisor),
                  fila(date(2026, 9, 30), "obligaciones_financieras_no_corrientes", 30.0, emisor),
                  fila(date(2026, 9, 30), "efectivo", 5.0, emisor),
                  fila(date(2026, 9, 30), "patrimonio_total", 40.0, emisor),
                  fila(date(2026, 9, 30), "patrimonio_controladora", 30.0, emisor)]
    m = metricas.fact_metrica_fundamental(pl.DataFrame(filas, schema=fundamentales.ESQUEMA))
    isa = {r["metrica"]: r["valor"] for r in m.filter((pl.col("emisor_id") == "CO-COE15")
                                                     & (pl.col("fecha_corte") == date(2026, 9, 30))).iter_rows(named=True)}
    # 12 meses a sep-2026: (7 + 9 − 6) + (0,9 + 1,2 − 0,75) = 10 + 1,35
    assert abs(isa["ebitda_12m"] - 11.35) < 1e-9
    assert isa["deuda_neta"] == 28.0 and isa["interes_minoritario"] == 10.0
    assert abs(isa["deuda_neta_ebitda"] - 28.0 / 11.35) < 1e-9
    assert m.filter((pl.col("emisor_id") == "CO-COT29") & (pl.col("metrica") == "ebitda_12m")).is_empty()
    v = pl.DataFrame({"fecha": [date(2026, 11, 20)] * 2, "especie": ["a", "b"], "emisor_id": ["CO-COE15"] * 2,
                      "capitalizacion": [50.0, 10.0], "trm": [4000.0] * 2})
    r = metricas.valor_empresa(v, m.with_columns(pl.col("fecha_corte").alias("fecha_disponible")))
    assert r.get_column("ev").to_list() == [98.0, 98.0]  # 60 de capitalización (dos clases) + 28 + 10
    assert abs(r.get_column("ev_ebitda").to_list()[0] - 98.0 / 11.35) < 1e-9


def test_bancos_desde_el_cuif_en_los_diciembres_sin_anexo():
    def f(corte, concepto, valor, emisor="CO-COB01"):
        return {"emisor_id": emisor, "fecha_corte": corte, "fecha_disponible": corte, "concepto": concepto, "valor": valor,
                "unidad": "pesos_por_accion", "base": "separado", "fuente": "anexo_211"}
    anexos = pl.DataFrame([f(date(2025, 6, 30), "acciones_circulacion", 100.0), f(date(2025, 9, 30), "acciones_circulacion", 100.0),
                           f(date(2025, 6, 30), "valor_patrimonial_accion", 50.0), f(date(2025, 9, 30), "valor_patrimonial_accion", 52.0),
                           f(date(2025, 6, 30), "utilidad_accion", 9.0), f(date(2025, 9, 30), "utilidad_accion", 3.0)],
                          schema=fundamentales.ESQUEMA)
    cuif = pl.DataFrame({"fecha_corte": [date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)] * 2,
                         "codigo_entidad": ["1"] * 6, "cuenta": ["300000"] * 3 + ["590000"] * 3,
                         "valor": [5000.0, 5200.0, 5500.0, 400.0, 600.0, 800.0]})
    mapeo = [{"emisor_id": "CO-COB01", "entidades": ["1"], "holding": False},
             {"emisor_id": "CO-COT29", "entidades": ["1"], "holding": True}]
    r = fundamentales.desde_cuif(anexos, cuif, mapeo)
    # El valor patrimonial derivado repite el del anexo (50 y 52): se usa en dic-2025. La utilidad derivada
    # (4 y 6) no repite la del anexo (9 y 3): no se usa
    assert r.select("emisor_id", "fecha_corte", "concepto", "valor", "fuente").rows() == [
        ("CO-COB01", date(2025, 12, 31), "valor_patrimonial_accion", 55.0, "cuif_derivado")]
    assert r.get_column("fecha_disponible").to_list() == [date(2026, 2, 19)]


def _celda_upa(emisor, corte, formato, concepto, valor, columna="VALOR"):
    return {"emisor_id": emisor, "fecha_corte": corte, "consolidado": False, "grupo": "trimestrales", "formato": formato,
            "fila": 1, "concepto": concepto, "columna": columna, "valor": valor}


def test_perdida_sola_con_signo_y_sin_signo():
    d = date(2024, 12, 31)
    anexos = pl.DataFrame([
        # ETB: el 187 escribe la pérdida negativa → pérdida
        _celda_upa("CO-COI13", d, "187", "PERDIDA POR ACCION", -25.69),
        # Conconcreto: el 141 la escribe positiva → queda sin signo hasta confirmar
        _celda_upa("CO-COF02", d, "141", "Pérdida por acción", 172.61),
        # El 211 con solo la fila de pérdida no se usa (ETB jun-2026 traía ahí una utilidad)
        _celda_upa("CO-COT04", d, "211", "PERDIDA POR ACCION", 7.51, "Número de accionistas"),
        # Utilidad y pérdida en el mismo anexo: la pérdida resta en valor absoluto
        _celda_upa("CO-COO08", d, "187", "UTILIDAD POR ACCION", 10.0),
        _celda_upa("CO-COO08", d, "187", "PERDIDA POR ACCION", -4.0),
    ])
    f = fundamentales.desde_anexos(anexos)
    upa = dict(f.filter(pl.col("concepto") == "utilidad_accion").select("emisor_id", "valor").iter_rows())
    assert upa == {"CO-COI13": -25.69, "CO-COO08": 6.0}
    assert f.filter(pl.col("concepto") == "utilidad_accion_sin_signo").get_column("emisor_id").to_list() == ["CO-COF02"]
    # Supersociedades confirma la pérdida de Conconcreto en 2024; sin confirmación se descarta
    ss_fila = {"emisor_id": "CO-COF02", "fecha_corte": d, "fecha_disponible": d, "concepto": "utilidad_total",
               "valor": -2e11, "unidad": "pesos", "base": "separado_anual", "fuente": "supersociedades"}
    con = fundamentales.con_signo_confirmado(pl.concat([f, pl.DataFrame([ss_fila], schema=fundamentales.ESQUEMA)]))
    assert con.filter((pl.col("emisor_id") == "CO-COF02") & (pl.col("concepto") == "utilidad_accion")) \
        .get_column("valor").to_list() == [-172.61]
    assert fundamentales.con_signo_confirmado(f).filter(pl.col("emisor_id") == "CO-COF02").is_empty()


def test_utilidad_por_accion_repetida_es_celda_sin_actualizar():
    # Grupo Aval mar-2018: el 211 repite los 89,82 de todo 2017; el 141 dice 26,52
    anexos = pl.DataFrame([
        _celda_upa("CO-COT29", date(2017, 12, 31), "211", "UTILIDAD POR ACCION", 89.82, "Número de accionistas"),
        _celda_upa("CO-COT29", date(2018, 3, 31), "211", "UTILIDAD POR ACCION", 89.82, "Número de accionistas"),
        _celda_upa("CO-COT29", date(2018, 3, 31), "141", "Utilidad por acción", 26.52),
        # Menos de un peso se repite por redondeo (BHI en dólares): se conserva
        _celda_upa("CO-PAT07", date(2024, 6, 30), "141", "Utilidad por acción", 0.01),
        _celda_upa("CO-PAT07", date(2024, 9, 30), "141", "Utilidad por acción", 0.01),
    ])
    f = fundamentales.desde_anexos(anexos).filter(pl.col("concepto") == "utilidad_accion").sort("emisor_id", "fecha_corte")
    assert f.select("emisor_id", "valor").rows() == [("CO-COT29", 89.82), ("CO-COT29", 26.52), ("CO-PAT07", 0.01), ("CO-PAT07", 0.01)]


def test_anio_reportado_por_trimestre_se_acumula_y_supersociedades_completa_diciembre():
    def fila(corte, concepto, valor, fuente, base="separado", unidad="pesos_por_accion"):
        return {"emisor_id": "CO-COG20", "fecha_corte": corte, "fecha_disponible": corte, "concepto": concepto,
                "valor": valor, "unidad": unidad, "base": base, "fuente": fuente}
    # Terpel 2025: cuatro trimestres sueltos que suman lo que da Supersociedades
    df = pl.DataFrame([
        fila(date(2025, 3, 31), "utilidad_accion", 919.83, "anexo_141"),
        fila(date(2025, 6, 30), "utilidad_accion", 879.55, "anexo_141"),
        fila(date(2025, 9, 30), "utilidad_accion", 955.72, "anexo_141"),
        fila(date(2025, 12, 31), "utilidad_accion", 709.07, "anexo_141"),
        fila(date(2025, 12, 31), "acciones_circulacion", 181424505.0, "anexo_211", unidad="acciones"),
        fila(date(2025, 12, 31), "utilidad_total", 6.3e11, "supersociedades", base="separado_anual", unidad="pesos"),
        fila(date(2024, 12, 31), "acciones_circulacion", 181424505.0, "anexo_211", unidad="acciones"),
        fila(date(2024, 12, 31), "utilidad_total", 5.3e11, "supersociedades", base="separado_anual", unidad="pesos"),
    ], schema=fundamentales.ESQUEMA)
    acum = fundamentales.acumular_trimestrales(df).filter(pl.col("concepto") == "utilidad_accion").sort("fecha_corte")
    assert [round(v, 2) for v in acum.get_column("valor").to_list()] == [919.83, 1799.38, 2755.1, 3464.17]
    # 2024 no tiene utilidad por acción en los anexos: diciembre sale de Supersociedades
    extra = fundamentales.upa_desde_supersociedades(df)
    assert extra.select("fecha_corte", "fuente").rows() == [(date(2024, 12, 31), "supersociedades_derivado")]
    assert round(extra.item(0, "valor"), 2) == 2921.33
