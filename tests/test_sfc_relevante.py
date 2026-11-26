from datetime import date, datetime, timezone

import polars as pl
import pytest

from invest import eventos, storage
from invest.ingest import sfc_relevante as rel
from relevante_util import SitioFalso, filas_reales, pagina_formulario, Respuesta

HOY = date(2026, 11, 22)


@pytest.fixture
def filas():
    return filas_reales()


@pytest.fixture
def especies(carpeta_datos):
    """Índice de especies mínimo con nombres tal como los publica la SFC."""
    datos = [
        ("cot23paao003", "AO GRUPO BOLIVAR S.A. ANTES SOCIEDADES BOLIVAR S.A.", "cot23", "ordinaria"),
        ("cot23paad006", "ADP GRUPO BOLIVAR S.A.", "cot23", "preferencial"),
        ("cog20paao006", "AO ORGANIZACION TERPEL S.A.", "cog20", "ordinaria"),
        ("cot09paao003", "AO GRUPO ARGOS S. A (ANT. INV. ARGOS - CIA DE CEMENTOS ARG S.A.)", "cot09", "ordinaria"),
        ("cot09paae038", "DERECHOS ADP SIN DERECHO A DIVIDENDO -GRUPO ARGOS ESPECIE SDPFGR", "cot09", "derechos"),
        ("coi13paao007", "AO EMPRESA DE TELECOMUNICACIONES DE BOGOTA S.A. ESP", "coi13", "ordinaria"),
        ("coe02paae009", "AO SIN DIVIDENDO  CIA. DE ELECTRICIDAD TULUA S.A. E.S.P.", "coe02", "sin_dividendo"),
    ]
    df = pl.DataFrame(
        [{"fecha_reporte": date(2026, 11, 18), "especie": e, "nombre": n, "emisor": em, "clase": c,
          "fuente_id": "sfc_especies", "ingest_ts": datetime(2026, 11, 22, tzinfo=timezone.utc)}
         for e, n, em, c in datos]
    )
    storage.upsert_por_anio("silver", "dim_especie_sfc", df, ("fecha_reporte", "especie"), "fecha_reporte")
    return df


# ------------------------------------------------------------------------- lectura de filas
def test_lee_las_filas_reales(filas):
    leidas = rel.interpretar_filas("".join(filas))
    assert len(leidas) == 9
    bolivar = next(f for f in leidas if f["fila"] == 463)
    assert bolivar["fecha_txt"] == "26/03/2026" and bolivar["hora"] == "16:14:24"
    assert bolivar["entidad"] == "GRUPO BOLIVAR S.A. (ANTES SOCIEDADES BOLIVAR S.A.)"
    assert bolivar["anexo_control"] == "formaIngreso:j_id_d:463:j_id_p"
    sin_anexo = next(f for f in leidas if f["fila"] == 5)
    assert sin_anexo["anexo_control"] is None and "CAÑA" in sin_anexo["entidad"]
    fiduciaria = next(f for f in leidas if f["fila"] == 9)
    assert fiduciaria["entidad"].endswith('"FIDUCIARIA BANCOLOMBIA"')


@pytest.mark.parametrize(
    "fila, categoria",
    [(350, "asamblea_decision"), (463, "ex_dividendo"), (422, "dividendo"), (680, "suspension"),
     (519, "readquisicion"), (103, "asamblea_convocatoria"), (5, "resultados"), (269, "gobierno"),
     (9, "otros")],
)
def test_clasifica_las_filas_reales(filas, fila, categoria):
    f = next(x for x in rel.interpretar_filas("".join(filas)) if x["fila"] == fila)
    assert eventos.clasificar(f["tema"], f["resumen"]) == categoria


def test_otras_reglas_de_clasificacion():
    assert eventos.clasificar("Ofertas Publicas de Adquisición", "") == "opa"
    assert eventos.clasificar("Otros eventos.", "La sociedad informa la aprobación de la fusión por absorción") \
        == "escision_fusion"
    assert eventos.clasificar("Proyecto Utilidad o Perdida aprobado por Asamblea          ", "") == "dividendo"
    assert eventos.clasificar("Informes de fin de Ejercicio.",
                              "aprobó la NO distribución de dividendos ordinarios") == "dividendo"


# ------------------------------------------------------------------------- enlace con el emisor
def test_enlaza_entidad_con_emisor(especies):
    tabla = eventos.tabla_emisores(especies)
    assert eventos.emisor_de("GRUPO BOLIVAR S.A. (ANTES SOCIEDADES BOLIVAR S.A.)", tabla) == "cot23"
    assert eventos.emisor_de("GRUPO ARGOS S.A.", tabla) == "cot09"
    assert eventos.emisor_de("ORGANIZACION TERPEL S.A.", tabla) == "cog20"
    # Mismo nombre que la especie sin dividendo de Tuluá, con tilde distinta
    assert eventos.emisor_de("COMPAÑIA DE ELECTRICIDAD TULUA S.A. E.S.P.", tabla) is None
    assert eventos.emisor_de("CIA. DE ELECTRICIDAD TULUA S.A. E.S.P.", tabla) == "coe02"
    assert eventos.emisor_de("FIDUCIARIA BANCOLOMBIA S.A.", tabla) is None
    alias = {eventos.clave_nombre("COMPAÑIA DE ELECTRICIDAD TULUA S.A. E.S.P."): "coe02"}
    assert eventos.emisor_de("COMPAÑIA DE ELECTRICIDAD TULUA S.A. E.S.P.", tabla, alias) == "coe02"


# ------------------------------------------------------------------------- cliente del formulario
def test_consulta_corta_no_pagina(filas):
    sitio = SitioFalso(filas)
    leidas, ctx, _ = rel.ClienteRelevante(sitio).consultar(date(2026, 3, 1), date(2026, 3, 31))
    assert len(leidas) == 9
    metodos = [(m, u.rsplit("/", 1)[-1]) for m, u, _, _ in sitio.pedidos]
    assert metodos == [("GET", "consulta.xhtml"), ("POST", "consulta.xhtml;jsessionid=SESION")]
    busqueda = sitio.pedidos[1][2]
    assert busqueda["formaIngreso:fecini_input"] == "01/03/2026"
    assert busqueda["formaIngreso:tema_input"] == "Todos"
    assert busqueda["javax.faces.ViewState"] == "VS-FORM"
    assert ctx.accion.endswith("busqueda.xhtml") and ctx.view_state == "VS-RES"


def test_consulta_larga_pide_todo_por_paginacion(filas):
    muchas = []
    for k in range(3):  # 27 filas con índices distintos
        muchas += [f.replace('data-ri="', f'data-ri="{k}0') for f in filas]
    sitio = SitioFalso(muchas)
    leidas, ctx, crudo = rel.ClienteRelevante(sitio).consultar(date(2026, 3, 1), date(2026, 3, 31))
    assert len(leidas) == 27
    ajax = sitio.pedidos[2]
    assert ajax[2]["javax.faces.partial.ajax"] == "true"
    assert ajax[2]["formaIngreso:j_id_d_rows"] == "2000"
    assert ajax[2]["javax.faces.ViewState"] == "VS-RES"
    assert ajax[3]["Faces-Request"] == "partial/ajax"
    assert ctx.view_state == "VS-AJAX"  # el anexo se pide con el estado más reciente
    assert "<partial-response" in crudo


def test_formulario_cambiado_se_informa():
    def sitio(metodo, url, **kw):
        return Respuesta("<html>mantenimiento</html>") if metodo == "POST" else Respuesta(pagina_formulario())
    with pytest.raises(rel.ErrorFormulario):
        rel.ClienteRelevante(sitio).consultar(date(2026, 3, 1), date(2026, 3, 31))


def test_ventanas_mensuales():
    assert rel.ventanas_mensuales(date(2026, 1, 15), date(2026, 3, 2)) == [
        (date(2026, 1, 15), date(2026, 1, 31)), (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 2))]


# ------------------------------------------------------------------------- conector de punta a punta
def test_conector_guarda_eventos_y_anexos_de_dividendos(carpeta_datos, especies, filas, monkeypatch):
    monkeypatch.setattr(rel, "_hoy", lambda: HOY)
    sitio = SitioFalso(filas)
    res = rel.ConectorSFCRelevante(rel.ClienteRelevante(sitio)).ejecutar(date(2026, 3, 1), date(2026, 3, 31))
    assert res.estado == "ok", res.resumen()
    assert res.filas_validas == 9 and "2 anexos guardados" in res.detalle
    df = storage.leer_tabla("silver", "fact_evento_sfc")
    con_anexo = df.filter(pl.col("anexo_archivo").is_not_null()).sort("entidad")
    # Bolívar (ex-dividendo) y Terpel (dividendo); Argos es readquisición y no baja anexo
    assert con_anexo.get_column("entidad").to_list() == [
        "GRUPO BOLIVAR S.A. (ANTES SOCIEDADES BOLIVAR S.A.)", "ORGANIZACION TERPEL S.A."]
    archivo = carpeta_datos / "anexos" / con_anexo.get_column("anexo_archivo")[0]
    assert archivo.read_bytes().startswith(b"%PDF") and archivo.name.endswith("_FECHAS_EXDIVIDENDO_GRUPO_BOLIVAR_2026.pdf")

    # Segunda corrida del mismo mes: no repite descargas, conserva las rutas y no duplica eventos
    sitio.pedidos.clear()
    rel.ConectorSFCRelevante(rel.ClienteRelevante(sitio)).ejecutar(date(2026, 3, 1), date(2026, 3, 31))
    assert not any(k.endswith(":j_id_p") for _, _, d, _ in sitio.pedidos for k in d)
    df2 = storage.leer_tabla("silver", "fact_evento_sfc")
    assert df2.height == 9 and df2.get_column("anexo_archivo").drop_nulls().len() == 2

    gold = eventos.construir_gold()
    por_entidad = {r["entidad"]: r for r in gold.iter_rows(named=True)}
    terpel = por_entidad["ORGANIZACION TERPEL S.A."]
    assert (terpel["categoria"], terpel["emisor_id"], terpel["afecta_precio"]) == ("dividendo", "CO-COG20", True)
    assert por_entidad["EMPRESA DE TELECOMUNICACIONES DE BOGOTA S.A. ESP"]["emisor_id"] == "CO-COI13"
    assert por_entidad["FONDO DE CAPITAL PRIVADO TC LATAM ESTRUCTURAS INMOBILIARIAS"]["emisor_id"] is None


def test_anexos_viejos_no_se_bajan(carpeta_datos, especies, filas, monkeypatch):
    monkeypatch.setattr(rel, "_hoy", lambda: date(2028, 1, 1))  # marzo de 2026 queda a más de 400 días
    sitio = SitioFalso(filas)
    res = rel.ConectorSFCRelevante(rel.ClienteRelevante(sitio)).ejecutar(date(2026, 3, 1), date(2026, 3, 31))
    assert res.filas_validas == 9 and "anexos" not in res.detalle


def test_anexos_pendientes_se_bajan_en_corridas_siguientes(carpeta_datos, especies, filas, monkeypatch):
    monkeypatch.setattr(rel, "_hoy", lambda: HOY)
    monkeypatch.setattr(rel, "MAX_ANEXOS_POR_CORRIDA", 1)  # la primera corrida llega al tope
    sitio = SitioFalso(filas)
    primera = rel.ConectorSFCRelevante(rel.ClienteRelevante(sitio)).ejecutar(date(2026, 3, 1), date(2026, 3, 31))
    assert "1 anexos guardados" in primera.detalle

    # Corrida siguiente sobre noviembre: marzo no está en el rango, pero tiene un PDF pendiente
    sitio.pedidos.clear()
    segunda = rel.ConectorSFCRelevante(rel.ClienteRelevante(sitio)).ejecutar(date(2026, 11, 1), date(2026, 11, 22))
    consultas = [d["formaIngreso:fecini_input"] for _, _, d, _ in sitio.pedidos if "formaIngreso:fecini_input" in d]
    assert consultas == ["01/11/2026", "01/03/2026"]
    assert "1 meses consultados de nuevo por anexos pendientes" in segunda.detalle
    assert "1 anexos guardados" in segunda.detalle
    df = storage.leer_tabla("silver", "fact_evento_sfc")
    assert df.get_column("anexo_archivo").drop_nulls().len() == 2

    # Ya no queda nada pendiente: la tercera corrida no vuelve a marzo
    sitio.pedidos.clear()
    tercera = rel.ConectorSFCRelevante(rel.ClienteRelevante(sitio)).ejecutar(date(2026, 11, 1), date(2026, 11, 22))
    assert "anexos pendientes" not in tercera.detalle


# ------------------------------------------------------------------------- estados financieros
@pytest.mark.parametrize("resumen, publicado, corte", [
    ("se publica el informe periódico trimestral, con corte al 30 de Junio de 2026.", date(2026, 8, 14), date(2026, 6, 30)),
    ("se permite publicar el informe periódico correspondiente al primer trimestre del 2026.", date(2026, 5, 15), date(2026, 3, 31)),
    ("Se radica el informe periódico correspondiente al tercer trimestre del año 2025", date(2025, 11, 15), date(2025, 9, 30)),
    ("Radicamos el Informe Periódico de Fin de Ejercicio 2025.", date(2026, 4, 13), date(2025, 12, 31)),
    ("informe periódico de fin de ejercico correspondiente al año 2024", date(2025, 4, 9), date(2024, 12, 31)),
    ("Se envía el informe financiero que contiene: estados financieros junto con las notas", date(2026, 3, 13), date(2025, 12, 31)),
])
def test_corte_de_los_estados_financieros(resumen, publicado, corte):
    assert rel.corte_de_resumen(resumen, publicado) == corte


def test_que_cuenta_como_estados_financieros():
    tema = "Informes de fin de Ejercicio."
    assert rel.es_informe_financiero(tema, "se publica el informe periódico trimestral, con corte al 30 de junio de 2026")
    assert rel.es_informe_financiero(tema, "Se envía el informe financiero que contiene: estados financieros, notas, "
                                           "proyecto de distribución de utilidades, dictamen del revisor fiscal")
    assert not rel.es_informe_financiero(tema, "Se radica Proyectos de distribución de utilidades año 2025.")
    assert not rel.es_informe_financiero(tema, "publica el proyecto del capítulo sobre los asuntos sociales y ambientales")
    assert not rel.es_informe_financiero("Decisiones de asamblea", "se publica el informe periódico trimestral")


def test_anexo_de_estados_solo_para_los_emisores_marcados():
    emisores = {eventos.clave_nombre("AO ORGANIZACION TERPEL S.A.", es_especie=True): "cog20",
                eventos.clave_nombre("AO GRUPO ARGOS S.A.", es_especie=True): "cot09"}
    tema, resumen = "Informes de fin de Ejercicio.", "se publica el informe periódico trimestral, con corte al 30 de junio de 2026"
    hoy, fecha = date(2026, 11, 25), date(2026, 8, 14)
    assert rel.anexo_deseado(tema, resumen, "ORGANIZACION TERPEL S.A.", fecha, hoy, emisores, {}, {"CO-COG20"})
    assert not rel.anexo_deseado(tema, resumen, "GRUPO ARGOS S.A.", fecha, hoy, emisores, {}, {"CO-COG20"})
    assert not rel.anexo_deseado(tema, resumen, "ORGANIZACION TERPEL S.A.", fecha, date(2029, 1, 1), emisores, {}, {"CO-COG20"})
