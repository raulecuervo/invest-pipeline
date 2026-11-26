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
    monkeypatch.setattr(bvc, "leer_pagina", lambda url=None, visible=None, fechas=(): datos)
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
    monkeypatch.setattr(bvc, "leer_pagina", lambda url=None, visible=None, fechas=(): datos)
    res = bvc.ConectorBVCIndices(ahora=datetime(2026, 11, 23, 19, 47, tzinfo=BOG)).ejecutar()
    assert res.estado == "ok", res.detalle
    df = storage.leer_tabla("silver", "fact_serie_macro").filter(pl.col("serie_id") == "msci_colcap").sort("fecha")
    # El viernes 20 es la sesión anterior al lunes 23
    assert df.select("fecha", "valor").rows() == [(date(2026, 11, 20), 2299.41), (date(2026, 11, 23), 2345.81)]


def test_sin_consola_no_abre_la_pagina(carpeta_datos, monkeypatch):
    # Lectura manual: en una tarea programada (sin consola) no se abre la página de la BVC
    monkeypatch.setattr(bvc, "hay_consola", lambda: False)
    abierta = []
    monkeypatch.setattr(bvc, "leer_pagina", lambda url=None, visible=None, fechas=(): abierta.append(url))
    res = bvc.ConectorBVC(ahora=datetime(2026, 11, 23, 19, 0, tzinfo=BOG)).ejecutar()
    assert res.estado == "error" and "manual" in res.detalle and not abierta


def test_reconoce_el_rechazo_del_cdn():
    assert bvc.es_bloqueo(403)
    assert bvc.es_bloqueo(200, "ERROR: The request could not be satisfied")
    assert not bvc.es_bloqueo(200, "Mercado local en línea | bvc", "Acciones Renta variable")


def test_bloqueo_no_se_reintenta_ni_se_pide_otra_pagina(carpeta_datos, monkeypatch):
    # Un rechazo de la Bolsa detiene la corrida: el segundo conector no vuelve a pedir la página
    monkeypatch.setattr(bvc, "_bloqueo_previo", None)
    pedidas = []

    def rechazo(url=None, visible=None, fechas=()):
        pedidas.append(url)
        raise bvc.BloqueoBVC("la Bolsa rechazó la lectura (HTTP 403); no se reintenta")

    monkeypatch.setattr(bvc, "leer_pagina", rechazo)
    ahora = datetime(2026, 11, 23, 19, 0, tzinfo=BOG)
    r1 = bvc.ConectorBVC(ahora=ahora).ejecutar()
    r2 = bvc.ConectorBVCIndices(ahora=ahora).ejecutar()
    assert r1.estado == "error" and "403" in r1.detalle
    assert r2.estado == "error" and "rechazó la lectura anterior" in r2.detalle
    assert len(pedidas) == 1


def test_boton_del_aplicativo_cuenta_como_lectura_manual(monkeypatch):
    monkeypatch.undo()  # sin el arreglo automático de consola
    monkeypatch.setattr(bvc.sys, "stdin", None)
    monkeypatch.delenv("INVEST_LECTURA_MANUAL", raising=False)
    assert not bvc.hay_consola()
    monkeypatch.setenv("INVEST_LECTURA_MANUAL", "1")
    assert bvc.hay_consola()


def test_recupera_dias_perdidos_con_el_filtro(carpeta_datos, monkeypatch):
    # Lectura del martes 24 a las 19:47: el lunes 23 ya está; faltan el viernes 20 y los anteriores
    pedidas = {}

    def leer(url=None, visible=None, fechas=()):
        pedidas["fechas"] = list(fechas)
        dia = {"hoy": "HOY 2026/11/24", "estado": "Mercado cerrado", "encabezado": ENCABEZADO, "filas": FILAS[:2]}
        dia["previos"] = {"2026-11-20": {**pagina(), "filas": [FILAS[0]]},
                          "2026-11-19": {"error": "la tabla no cambió: la Bolsa no entregó ese día"}}
        return dia

    correr(monkeypatch, datetime(2026, 11, 23, 19, 47, tzinfo=BOG), pagina())  # deja el 23
    monkeypatch.setattr(bvc, "leer_pagina", leer)
    res = bvc.ConectorBVC(ahora=datetime(2026, 11, 24, 19, 47, tzinfo=BOG)).ejecutar()
    assert pedidas["fechas"][0] == date(2026, 11, 20) and date(2026, 11, 23) not in pedidas["fechas"]
    assert len(pedidas["fechas"]) == bvc.DIAS_RECUPERAR - 1
    assert "días recuperados: 2026-11-20" in res.detalle and "2026-11-19 no se recuperó" in res.detalle
    df = storage.leer_tabla("silver", "fact_precio_bvc")
    assert sorted(df.get_column("fecha").unique().to_list()) == [date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24)]


def test_antes_del_cierre_solo_recupera_dias_anteriores(carpeta_datos, monkeypatch):
    def leer(url=None, visible=None, fechas=()):
        return {**pagina(hoy="HOY 2026/11/24", estado="Mercado abierto"),
                "previos": {"2026-11-23": {**pagina(), "filas": [FILAS[0]]}}}

    monkeypatch.setattr(bvc, "leer_pagina", leer)
    res = bvc.ConectorBVC(ahora=datetime(2026, 11, 24, 11, 0, tzinfo=BOG)).ejecutar()
    df = storage.leer_tabla("silver", "fact_precio_bvc")
    assert df.get_column("fecha").unique().to_list() == [date(2026, 11, 23)]  # el 24 abierto no se guarda
    assert "solo días anteriores" in res.detalle


def test_precio_en_curso_se_guarda_aparte(carpeta_datos, monkeypatch):
    monkeypatch.setattr(bvc, "_bloqueo_previo", None)
    datos = pagina(hoy="HOY 2026/11/24", estado="Mercado abierto")
    monkeypatch.setattr(bvc, "leer_pagina", lambda url=None, visible=None, fechas=(): datos)
    res = bvc.ConectorBVCEnCurso(ahora=datetime(2026, 11, 24, 11, 40, tzinfo=BOG)).ejecutar()
    assert res.estado in ("ok", "revisar"), res.detalle
    df = storage.leer_tabla("silver", "fact_precio_bvc_en_curso")
    eco = df.filter(pl.col("nemotecnico") == "ECOPETROL").row(0, named=True)
    assert eco["cierre"] == 2340.0 and eco["hora_lectura"] == datetime(2026, 11, 24, 11, 40)
    assert eco["estado_mercado"] == "Mercado abierto"
    assert storage.leer_tabla("silver", "fact_precio_bvc") is None  # nunca como cierre
    # Una lectura posterior reemplaza a la anterior del mismo día
    bvc.ConectorBVCEnCurso(ahora=datetime(2026, 11, 24, 14, 5, tzinfo=BOG)).ejecutar()
    df = storage.leer_tabla("silver", "fact_precio_bvc_en_curso").filter(pl.col("nemotecnico") == "ECOPETROL")
    assert df.height == 1 and df.item(0, "hora_lectura") == datetime(2026, 11, 24, 14, 5)
    # Antes de que abra el mercado la página sigue con el día anterior: no se guarda
    res = bvc.ConectorBVCEnCurso(ahora=datetime(2026, 11, 25, 8, 0, tzinfo=BOG)).ejecutar()
    assert res.estado == "sin_novedades" and "no ha abierto" in res.detalle


def test_dia_que_la_bolsa_no_entrega_se_salta(carpeta_datos, monkeypatch):
    # La lógica del bucle de leer_pagina: un día con 417 se anota y se sigue; un 403 detiene
    js = bvc.JS_ELEGIR_FECHA
    assert "react-date-picker__calendar-button" in js and "Filtros" in js  # escritorio y pantalla angosta
    assert "saltar: st !== 403 && st !== 429" in js
