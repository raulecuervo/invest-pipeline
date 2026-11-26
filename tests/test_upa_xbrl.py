"""Utilidad por acción del XBRL donde el anexo falta o es inverosímil (Bolsa Mercantil, nov-2026)."""

from datetime import date

import polars as pl

from invest import fundamentales as fu


def _fila(f, concepto, valor, fuente, base="separado", unidad="pesos"):
    return {"emisor_id": "CO-COR02", "fecha_corte": f, "fecha_disponible": f, "concepto": concepto,
            "valor": valor, "unidad": unidad, "base": base, "fuente": fuente}


def _df(filas):
    return pl.DataFrame(filas, schema=fu.ESQUEMA)


def test_llena_diciembre_y_cambia_el_anexo_inverosimil():
    sep, dic, mar = date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31)
    filas = []
    for f, upa_x, util in ((sep, 505.0, 29.881e9), (dic, 672.0, 39.796e9), (mar, 179.0, 10.612e9)):
        filas += [_fila(f, "upa_basica", upa_x, "xbrl", "consolidado", "pesos_por_accion"),
                  _fila(f, "utilidad_controladora", util, "xbrl", "consolidado"),
                  _fila(f, "patrimonio_controladora", 104e9, "xbrl", "consolidado"),
                  _fila(f, "acciones_circulacion", 59_199_032.0, "anexo_211", unidad="acciones")]
    filas += [_fila(sep, "utilidad_accion", 1256.17, "anexo_211", unidad="pesos_por_accion"),   # inverosímil
              _fila(mar, "utilidad_accion", 179.25, "anexo_211", unidad="pesos_por_accion")]    # cuadra: se queda
    r = fu.con_upa_xbrl(_df(filas)).filter((pl.col("concepto") == "utilidad_accion") & (pl.col("base") == "separado"))
    por_corte = {f: (v, fu_) for f, v, fu_ in r.select("fecha_corte", "valor", "fuente").iter_rows()}
    assert por_corte == {sep: (505.0, "xbrl_upa"), dic: (672.0, "xbrl_upa"), mar: (179.25, "anexo_211")}


def test_xbrl_que_no_cuadra_con_su_utilidad_no_se_usa():
    dic = date(2022, 12, 31)
    filas = [_fila(dic, "upa_basica", 429627.0, "xbrl", "consolidado", "pesos_por_accion"),   # escala equivocada
             _fila(dic, "utilidad_controladora", 2.0e12, "xbrl", "consolidado"),
             _fila(dic, "acciones_circulacion", 580e6, "anexo_211", unidad="acciones")]
    r = fu.con_upa_xbrl(_df(filas))
    assert r.filter(pl.col("concepto") == "utilidad_accion").is_empty()
