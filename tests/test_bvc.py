from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from invest import storage
from invest.ingest import bvc

BOG = ZoneInfo("America/Bogota")
ENCABEZADO = list(bvc.COLUMNAS)
# Filas reales de la tabla "Mercado local en línea" del 23-nov-2026
FILAS = [
    ["ECOPETROL", "2,340.00", "1.08%", "49,345,537,805.00", "20,910,721.00", "25.00", "2,375.00", "2,390.00", "2,340.00", "-", "ECOPETROL"],
    ["PFCIBEST", "73,520.00", "3.55%", "39,949,887,640.00", "544,539.00", "2,520.00", "72,300.00", "74,000.00", "72,300.00", "-", "Grupo Cibest S.A."],
    ["ICOLCAP", "23,206.00", "1.23%", "23,192,944,804.00", "999,333.00", "282.00", "22,300.00", "23,300.00", "22,300.00", "-", "FONDO BURSATIL ISHARESCOLCAP"],
    ["PFCEMARGOS", "-", "0.00%", "24,223,480.00", "2,500.00", "-", "-", "-", "-", "-", "CEMENTOS ARGOS"],
    ["NUEVA", "10.00", "0%", "1.00", "1.00", "0", "10.00", "9.00", "11.00", "-", "EMISOR NUEVO"],  # máx < mín
]


def pagina(hoy="HOY 2026/11/23", estado="Mercado cerrado", encabezado=ENCABEZADO):
    return {"hoy": hoy, "estado": estado, "encabezado": encabezado, "filas": FILAS}


@pytest.fixture(autouse=True)
def consola(monkeypatch):
    monkeypatch.setattr(bvc, "hay_consola", lambda: True)


def correr(monkeypatch, ahora, datos):
    monkeypatch.setattr(bvc, "leer_pagina", lambda url=None, visible=None: datos)
    return bvc.ConectorBVC(ahora=ahora).ejecutar()


def test_numeros_y_fecha():
    assert bvc.numero("49,345,537,805.00") == 49345537805.0
    assert bvc.numero("1.08%") == 1.08 and bvc.numero("-") is None
    assert bvc.fecha_pagina("HOY 2026/11/23 Día bursátil") == date(2026, 11, 23)


def test_lee_la_tabla_despues_del_cierre(carpeta_datos, monkeypatch):
    res = correr(monkeypatch, datetime(2026, 11, 23, 19, 47, tzinfo=BOG), pagina())
    assert res.estado == "revisar", res.detalle  # la fila con máximo < mínimo va a cuarentena
    assert "NUEVA" in res.detalle  # nemotécnico sin equivalencia: avisado
    df = storage.leer_tabla("silver", "fact_precio_bvc").sort("nemotecnico")
    assert df.get_column("nemotecnico").to_list() == ["ECOPETROL", "ICOLCAP", "PFCEMARGOS", "PFCIBEST"]
    eco = df.filter(pl.col("nemotecnico") == "ECOPETROL").row(0, named=True)
    assert (eco["especie"], eco["cierre"], eco["maximo"], eco["minimo"], eco["volumen_cop"]) == (
        "coc04paao008", 2340.0, 2390.0, 2340.0, 49345537805.0)
    etf = df.filter(pl.col("nemotecnico") == "ICOLCAP").row(0, named=True)
    assert etf["especie"] is None and etf["clase"] == "etf"
    assert df.filter(pl.col("nemotecnico") == "PFCEMARGOS").item(0, "cierre") is None


@pytest.mark.parametrize("ahora, datos, motivo", [
    (datetime(2026, 11, 23, 15, 30, tzinfo=BOG), pagina(), "fuera del horario"),         # antes del cierre
    (datetime(2026, 11, 22, 19, 47, tzinfo=BOG), pagina(), "fuera del horario"),         # domingo
    (datetime(2026, 11, 24, 8, 0, tzinfo=BOG), pagina(), "fuera del horario"),           # PC encendido al otro día
    (datetime(2026, 11, 24, 19, 47, tzinfo=BOG), pagina(), "otra fecha"),                # la página no se actualizó
    (datetime(2026, 11, 23, 19, 47, tzinfo=BOG), pagina(estado="Mercado abierto"), "mercado abierto"),
])
def test_no_guarda_datos_de_otro_momento(carpeta_datos, monkeypatch, ahora, datos, motivo):
    res = correr(monkeypatch, ahora, datos)
    assert res.estado == "sin_novedades" and motivo in res.detalle
    assert storage.leer_tabla("silver", "fact_precio_bvc") is None


def test_avisa_si_la_tabla_cambia(carpeta_datos, monkeypatch):
    res = correr(monkeypatch, datetime(2026, 11, 23, 19, 47, tzinfo=BOG),
                 pagina(encabezado=[h for h in ENCABEZADO if h != "Precio máximo"]))
    assert res.estado == "error" and "Precio máximo" in res.detalle


def test_indices_hoy_y_ayer(carpeta_datos, monkeypatch):
    datos = {"hoy": "HOY 2026/11/23", "estado": "Mercado cerrado", "encabezado": bvc.ENCABEZADO_INDICES,
             "filas": [["MSCI NUAM USD", "1,643.11", "1,605.72", "37.39", "2.33%"],
                       ["MSCI COLCAP", "2,345.81", "2,299.41", "46.40", "2.02%"]]}
    monkeypatch.setattr(bvc, "leer_pagina", lambda url=None, visible=None: datos)
    res = bvc.ConectorBVCIndices(ahora=datetime(2026, 11, 23, 19, 47, tzinfo=BOG)).ejecutar()
    assert res.estado == "ok", res.detalle
    df = storage.leer_tabla("silver", "fact_serie_macro").filter(pl.col("serie_id") == "msci_colcap").sort("fecha")
    # El viernes 20 es la sesión anterior al lunes 23
    assert df.select("fecha", "valor").rows() == [(date(2026, 11, 20), 2299.41), (date(2026, 11, 23), 2345.81)]


def test_sin_consola_no_abre_la_pagina(carpeta_datos, monkeypatch):
    # Lectura manual: en una tarea programada (sin consola) no se abre la página de la BVC
    monkeypatch.setattr(bvc, "hay_consola", lambda: False)
    abierta = []
    monkeypatch.setattr(bvc, "leer_pagina", lambda url=None, visible=None: abierta.append(url))
    res = bvc.ConectorBVC(ahora=datetime(2026, 11, 23, 19, 0, tzinfo=BOG)).ejecutar()
    assert res.estado == "error" and "manual" in res.detalle and not abierta
