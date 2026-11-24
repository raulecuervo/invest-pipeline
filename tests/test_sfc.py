import zipfile
from datetime import date

import polars as pl
import pytest

from invest import dims, storage
from invest.ingest import http, sfc
from sfc_util import CIBEST_2026, ECOPETROL_2026, ECOPETROL_ANT, FIXTURES, ServidorFalso, escribir_biff2

HOY = date(2026, 11, 22)


@pytest.fixture
def indice_html():
    return sfc.decodificar((FIXTURES / "ppal_promedios.htm").read_bytes())


@pytest.fixture
def ficha_html():
    return sfc.decodificar((FIXTURES / "d_coc04paao008.htm").read_bytes())


@pytest.fixture
def servidor(monkeypatch):
    archivos = {
        "ppal_promedios.htm": (FIXTURES / "ppal_promedios.htm").read_bytes(),
        "d_coc04paao008.htm": (FIXTURES / "d_coc04paao008.htm").read_bytes(),
        "coc04paao008_2026.xls": escribir_biff2(ECOPETROL_2026),
        "coc04paao008_ant.xls": escribir_biff2(ECOPETROL_ANT),
        "cotk7paao003_2026.xls": escribir_biff2(CIBEST_2026),
    }
    falso = ServidorFalso(archivos)
    monkeypatch.setattr(sfc.http, "obtener", falso)
    monkeypatch.setattr(sfc.http, "obtener_condicional", falso.condicional)
    monkeypatch.setattr(sfc, "_hoy", lambda: HOY)
    return falso


# ------------------------------------------------------------------------- interpretación
def test_indice_lista_especies_con_clase_y_emisor(indice_html):
    r = sfc.interpretar_indice(indice_html)
    assert r["fecha_reporte"] == date(2026, 11, 18)
    por_especie = {e["especie"]: e for e in r["especies"]}
    assert len(por_especie) == 15
    eco = por_especie["coc04paao008"]
    assert (eco["nombre"], eco["emisor"], eco["clase"]) == ("AO PEC ECOPETROL S.A.", "coc04", "ordinaria")
    assert por_especie["cotk7paad014"]["clase"] == "preferencial"
    assert por_especie["cob52paae000"]["clase"] == "sin_dividendo"
    assert por_especie["cob01paao006"]["nombre"] == "AO BANCO DE BOGOTA"
    assert por_especie["cot09paae038"]["clase"] == "derechos"
    assert por_especie["coe15paap009"]["clase"] == "privilegiada"
    assert por_especie["coe15paap009"]["nombre"].startswith("AO INTERCONEXION ELECTRICA")
    assert "CAÑA" in por_especie["coa39paao009"]["nombre"]
    assert por_especie["coo07paao007"]["nombre"] == "AO AZUL & BLANCO S.A."
    # Tres especies del mismo emisor (AV Villas) comparten el código de emisor
    assert {e["clase"] for e in r["especies"] if e["emisor"] == "cob52"} == {
        "ordinaria", "preferencial", "sin_dividendo"}


def test_ficha_lee_todos_los_bloques(ficha_html):
    f = sfc.interpretar_ficha(ficha_html, "coc04paao008")
    assert f["nombre"] == "AO PEC ECOPETROL S.A."
    assert f["codigo_accion"] == "COC04PAAO008"
    assert f["sector_sfc"] == "ENTIDADES PUBLICAS"
    assert f["clase_accion_sfc"] is None
    assert f["fecha_reporte"] == date(2026, 11, 18)
    assert f["fecha_ultima_operacion"] == date(2026, 11, 18)
    assert (f["precio_promedio"], f["fecha_precio"]) == (2410.81, date(2026, 11, 18))
    assert (f["maximo_2m"], f["fecha_maximo"]) == (2832.97, date(2026, 10, 15))
    assert (f["minimo_2m"], f["fecha_minimo"]) == (2410.81, date(2026, 11, 18))
    assert f["valor_patrimonial"] is None  # la SFC escribe 1.00 cuando no lo tiene
    assert f["q_tobin"] == 2410.81 and f["rpg"] == 2410.81
    assert f["liquidez_bursatil"] == 3249954100.0
    assert (f["iba_categoria"], f["iba"], f["fecha_iba"]) == ("ALT", 10.0, date(2026, 10, 31))


@pytest.mark.parametrize(
    "texto, esperado", [("2,410.81", 2410.81), (" N.A ", None), ("", None), ("3249954100.0%", 3249954100.0)]
)
def test_numeros_con_formato_de_la_sfc(texto, esperado):
    assert sfc.numero(texto) == esperado


def test_fechas():
    assert sfc.fecha_dmy("24/03/2026") == date(2026, 3, 24)
    assert sfc.fecha_dmy(46105.0) == date(2026, 3, 24)  # número de serie de Excel
    assert sfc.fecha_dmy("31/02/2026") is None


# ------------------------------------------------------------------------- conectores
def test_carga_historica_de_precios(carpeta_datos, servidor):
    res = sfc.ConectorSFCPrecios().ejecutar(date(1990, 1, 1), date(2026, 11, 29))
    assert res.estado == "ok", res.resumen()  # los marcadores precio=1 son esperados
    df = storage.leer_tabla("silver", "fact_precio_sfc")
    eco = df.filter(pl.col("especie") == "coc04paao008").sort("fecha")
    # 10 filas de 2026 con una repetida + 2 válidas de años anteriores
    assert eco.height == 9 + 2
    assert eco.get_column("fecha").min() == date(2025, 12, 29)
    assert eco.filter(pl.col("fecha") == date(2026, 3, 24)).height == 1
    assert df.filter(pl.col("especie") == "cotk7paao003").height == 3
    # Los dos marcadores van a cuarentena, con su motivo
    cuarentena = pl.read_parquet(next((carpeta_datos / "cuarentena" / "fact_precio_sfc").glob("*.parquet")))
    assert cuarentena.get_column("motivo").unique().to_list() == ["marcador_sin_negociacion"]
    assert "marcador_sin_negociacion 2" in res.detalle
    # El crudo queda en un zip en la carpeta efímera (no versionada), con los archivos tal cual
    assert "_efimero" in res.archivo_bronze.parts
    with zipfile.ZipFile(res.archivo_bronze) as z:
        assert "coc04paao008_ant.xls" in z.namelist()
        assert z.read("coc04paao008_2026.xls") == servidor.archivos["coc04paao008_2026.xls"]
    # El índice se descargó una vez, al no existir aún
    assert servidor.pedidos.count("ppal_promedios.htm") == 1
    assert storage.leer_tabla("silver", "dim_especie_sfc").height == 15


def test_corrida_diaria_solo_pide_el_año_en_curso_y_omite_inactivas(carpeta_datos, servidor):
    sfc.ConectorSFCPrecios().ejecutar(date(1990, 1, 1), date(2026, 11, 29))
    # Una especie con última negociación hace más de 400 días se marca como inactiva
    viejo = pl.DataFrame({
        "fecha": [date(2020, 5, 4)], "especie": ["cob01paao006"], "codigo_titulo": ["COB01PAAO006"],
        "precio_promedio": [10.0], "valor_transado": [1000.0], "fuente_id": ["sfc_precios_promedio"],
        "ingest_ts": [None],
    }).with_columns(pl.col("ingest_ts").cast(pl.Datetime("us", "UTC")))
    storage.upsert_por_anio("silver", "fact_precio_sfc", viejo, ("fecha", "especie"))
    servidor.pedidos.clear()

    res = sfc.ConectorSFCPrecios().ejecutar(date(2026, 11, 13), date(2026, 11, 29))
    assert not any(p.endswith("_ant.xls") for p in servidor.pedidos)
    assert "cob01paao006_2026.xls" not in servidor.pedidos
    assert "1 especies inactivas omitidas" in res.detalle
    assert res.filas_validas == 3 + 2  # Ecopetrol: 13, 17 y 18 de nov.; Cibest: 17 y 18


def test_reejecutar_no_duplica(carpeta_datos, servidor):
    c = sfc.ConectorSFCPrecios
    c().ejecutar(date(2026, 1, 1), date(2026, 11, 29))
    n = storage.leer_tabla("silver", "fact_precio_sfc").height
    c().ejecutar(date(2026, 1, 1), date(2026, 11, 29))
    assert storage.leer_tabla("silver", "fact_precio_sfc").height == n


def test_fecha_repetida_con_valores_distintos_se_revisa():
    c = sfc.ConectorSFCPrecios()
    df = c.interpretar([
        {"especie": "coc04paao008", "codigo_titulo": "COC04PAAO008", "fecha": date(2026, 3, 24),
         "precio_promedio": 2708.2, "valor_transado": 1.0e9},
        {"especie": "coc04paao008", "codigo_titulo": "COC04PAAO008", "fecha": date(2026, 3, 24),
         "precio_promedio": 2710.0, "valor_transado": 2.0e9},
        {"especie": "coc04paao008", "codigo_titulo": "COTK7PAAO003", "fecha": date(2026, 3, 25),
         "precio_promedio": 2700.0, "valor_transado": 2.0e9},
    ])
    validas, cuarentena = c.validar(df)
    assert validas.is_empty()
    assert sorted(cuarentena.get_column("motivo").to_list()) == [
        "codigo_no_coincide", "fecha_duplicada_con_valores_distintos", "fecha_duplicada_con_valores_distintos"]


def test_ficha_y_dimension_de_activos(carpeta_datos, servidor):
    sfc.ConectorSFCPrecios().ejecutar(date(1990, 1, 1), date(2026, 11, 29))
    res = sfc.ConectorSFCFicha().ejecutar()
    # Solo existe la ficha de Ecopetrol en el servidor falso; las demás responden 404
    assert res.filas_validas == 1 and "14 fichas no disponibles" in res.detalle
    ficha = storage.leer_tabla("silver", "fact_indicadores_sfc")
    assert ficha.row(0, named=True)["iba_categoria"] == "ALT"

    activos = dims.dim_activo()
    eco = activos.filter(pl.col("activo_id") == "COC04PAAO008").row(0, named=True)
    assert eco["emisor_id"] == "CO-COC04" and eco["sector_sfc"] == "ENTIDADES PUBLICAS"
    assert eco["ultima_negociacion"] == date(2026, 11, 18) and eco["activa"] is True
    assert activos.height == 15


def test_acceso_denegado_detiene_la_ingesta(carpeta_datos, servidor):
    servidor.errores["coc04paao008_2026.xls"] = http.AccesoDenegado("403")
    res = sfc.ConectorSFCPrecios().ejecutar(date(2026, 1, 1), date(2026, 11, 29))
    assert res.estado == "error" and "AccesoDenegado" in res.detalle


# ------------------------------------------------------------------------- cliente HTTP
class _Respuesta:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class _Sesion:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)

    def request(self, metodo, url, data=None, headers=None, timeout=None):
        assert headers["User-Agent"]
        return self.respuestas.pop(0)


@pytest.mark.parametrize(
    "respuestas, resultado",
    [
        ([_Respuesta(500), _Respuesta(200, b"ok")], b"ok"),
        ([_Respuesta(404)], http.NoEncontrado),
        ([_Respuesta(403)], http.AccesoDenegado),
        ([_Respuesta(503)] * 4, http.ErrorHTTP),
    ],
)
def test_cliente_http(monkeypatch, respuestas, resultado):
    monkeypatch.setenv("INVEST_PAUSA_SEGUNDOS", "0")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "_sesion", _Sesion(respuestas))
    if isinstance(resultado, bytes):
        assert http.obtener("https://ejemplo/x") == resultado
    else:
        with pytest.raises(resultado):
            http.obtener("https://ejemplo/x")


# ------------------------------------------------------------------------- corridas incrementales
def _corrida_diaria():
    return [c().ejecutar() for c in (sfc.ConectorSFCEspecies, sfc.ConectorSFCFicha, sfc.ConectorSFCPrecios)]


def test_sin_reporte_nuevo_no_se_descarga_nada(carpeta_datos, servidor):
    primera = _corrida_diaria()
    assert [r.estado for r in primera] == ["ok", "ok", "ok"], [r.resumen() for r in primera]
    servidor.pedidos.clear()

    segunda = _corrida_diaria()
    assert [r.estado for r in segunda] == ["sin_novedades"] * 3, [r.resumen() for r in segunda]
    # Solo se consultó el índice, y respondió "sin cambios"
    assert servidor.pedidos == ["ppal_promedios.htm"]
    assert servidor.no_modificados == ["ppal_promedios.htm"]
    assert "sin reporte nuevo de la SFC" in segunda[2].detalle


def test_reporte_nuevo_baja_solo_lo_que_cambio(carpeta_datos, servidor):
    _corrida_diaria()
    antes = storage.leer_tabla("silver", "fact_precio_sfc").height
    # La SFC publica el reporte del 19/11: Ecopetrol negoció ese día; Cibest no tiene ficha (404)
    a = servidor.archivos
    a["ppal_promedios.htm"] = a["ppal_promedios.htm"].replace(b"18/11/2026", b"19/11/2026")
    a["d_coc04paao008.htm"] = a["d_coc04paao008.htm"].replace(b"18/11/2026", b"19/11/2026")
    a["coc04paao008_2026.xls"] = escribir_biff2(
        ECOPETROL_2026 + [("COC04PAAO008", "19/11/2026", 2450.5, 41000000000.0)])
    servidor.pedidos.clear()
    servidor.no_modificados.clear()

    especies, ficha, precios = _corrida_diaria()
    assert ficha.filas_validas == 1
    assert "coc04paao008_2026.xls" in servidor.pedidos
    assert not any(p.endswith("_ant.xls") for p in servidor.pedidos)
    assert servidor.no_modificados == ["cotk7paao003_2026.xls"]  # pedido, pero respondió 304
    assert "1 archivos sin cambios" in precios.detalle
    assert storage.leer_tabla("silver", "fact_precio_sfc").height == antes + 1


def test_especie_sin_operacion_nueva_ni_se_pide(carpeta_datos, servidor):
    _corrida_diaria()
    # Nuevo reporte, pero la ficha de Ecopetrol sigue diciendo última operación 18/11 (ya guardada)
    a = servidor.archivos
    a["ppal_promedios.htm"] = a["ppal_promedios.htm"].replace(b"18/11/2026", b"19/11/2026")
    a["d_coc04paao008.htm"] = a["d_coc04paao008.htm"].replace(
        b"de reporte : <font color=\"#666666\">18/11/2026", b"de reporte : <font color=\"#666666\">19/11/2026")
    servidor.pedidos.clear()
    _, _, precios = _corrida_diaria()
    assert "coc04paao008_2026.xls" not in servidor.pedidos
    assert "1 sin operación nueva" in precios.detalle


def test_corrida_fallida_no_actualiza_la_memoria(carpeta_datos, servidor):
    sfc.ConectorSFCEspecies().ejecutar()
    servidor.errores["coc04paao008_2026.xls"] = http.AccesoDenegado("403")
    assert sfc.ConectorSFCPrecios().ejecutar().estado == "error"
    assert storage.leer_estado_fuente("sfc_precios_promedio") == {}
    del servidor.errores["coc04paao008_2026.xls"]
    assert sfc.ConectorSFCPrecios().ejecutar().estado == "ok"  # vuelve a intentar todo


def test_desde_explicito_fuerza_la_descarga(carpeta_datos, servidor):
    _corrida_diaria()
    servidor.pedidos.clear()
    sfc.ConectorSFCPrecios().ejecutar(date(2026, 1, 1), date(2026, 11, 29))
    assert "coc04paao008_2026.xls" in servidor.pedidos and not servidor.no_modificados


def test_fechas_sin_cero_inicial():
    # Así vienen en coc04paao008_2025.xls
    assert sfc.fecha_dmy("4/11/2025") == date(2025, 11, 4) and sfc.fecha_dmy("1/2/2025") == date(2025, 2, 1)
