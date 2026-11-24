from datetime import date
from pathlib import Path

import polars as pl

from invest import storage
from invest.ingest import sfc_niif

MUESTRA = (Path(__file__).parent / "fixtures" / "sfc_niif_muestra.xbrl").read_bytes()
NOMBRE = "0054368214_0260_000036_0000_000000_000000_C-C_2025-12-31.xbrl"


def _por_concepto(hechos, concepto, fin):
    return [h for h in hechos if h["concepto"] == concepto and h["fecha_fin"] == fin]


def test_hechos_sin_dimensiones_y_duplicados():
    hechos = sfc_niif.hechos_xbrl(MUESTRA)
    activos = _por_concepto(hechos, "ifrs:Assets", date(2025, 12, 31))
    # El hecho con dimensión (segmento) se descarta; queda el total, en miles de pesos
    assert len(activos) == 1 and activos[0]["valor"] == 284136657544.0 and activos[0]["escala"] == "miles_de_pesos"
    assert _por_concepto(hechos, "ifrs:Assets", date(2024, 12, 31))[0]["valor"] == 301345177424.0  # comparativo
    # Repetido en el mismo contexto, uno en cero: queda la cifra
    assert _por_concepto(hechos, "ifrs:CashAndCashEquivalents", date(2025, 12, 31))[0]["valor"] == 10694387457.0
    upa = _por_concepto(hechos, "ifrs:BasicEarningsLossPerShare", date(2025, 12, 31))[0]
    assert upa["valor"] == 220 and upa["escala"] == "pesos" and upa["fecha_inicio"] == date(2025, 1, 1)
    assert not [h for h in hechos if h["concepto"] == "sfc:NombreEntidad"]  # texto, no cifra


def test_cortes_y_nombre_de_archivo():
    assert sfc_niif.cortes(date(2014, 1, 1), date(2015, 10, 1)) == [date(2015, 9, 30), date(2015, 6, 30), date(2015, 3, 31)]
    assert sfc_niif.datos_archivo(NOMBRE) == {"radicado": "0054368214", "tipo_informe": "consolidado",
                                              "fecha_corte": date(2025, 12, 31)}


ANEXOS = {
    "trimestrales": {
        "formatos": [{"codigoFormato": "141", "nombre": "INFORMACION ECONOMICA GENERAL", "maximoColumnas": "2"}],
        "columnasFormatos": [{"codigoFormato": "141", "codigoColumna": "0", "nombreColumna": "DESCRIPCIÓN"},
                             {"codigoFormato": "141", "codigoColumna": "1", "nombreColumna": "VALOR"}],
        "detalleFormatos": [
            {"codigoFormato": "141", "numColumna": "0", "valorColumna": "Valor patrimonial x acción sin valorización"},
            {"codigoFormato": "141", "numColumna": "1", "valorColumna": "10,888.62"},
            {"codigoFormato": "141", "numColumna": "0", "valorColumna": "Utilidad por acción"},
            {"codigoFormato": "141", "numColumna": "1", "valorColumna": "1,765.09"}]},
    "semestrales": {
        "formatos": [{"codigoFormato": "211", "nombre": "NUMERO DE ACCIONES Y NUMERO DE ACCIONISTAS", "maximoColumnas": "3"}],
        "columnasFormatos": [{"codigoFormato": "211", "codigoColumna": "0", "nombreColumna": "CONCEPTO"},
                             {"codigoFormato": "211", "codigoColumna": "1", "nombreColumna": "NÚMERO DE ACCIONISTAS"},
                             {"codigoFormato": "211", "codigoColumna": "2", "nombreColumna": "NÚMERO DE ACCIONES"}],
        "detalleFormatos": [
            {"codigoFormato": "211", "numColumna": "0", "valorColumna": "ACCIONES ORDINARIAS"},
            {"codigoFormato": "211", "numColumna": "1", "valorColumna": "281,965.00"},
            {"codigoFormato": "211", "numColumna": "2", "valorColumna": "41,116,694,690.00"}]},
}


def test_anexos_aplanados():
    filas = sfc_niif.filas_anexos(ANEXOS, consolidado=False)
    upa = [f for f in filas if f["concepto"] == "Utilidad por acción"][0]
    assert upa["valor"] == 1765.09 and upa["formato"] == "141" and upa["fila"] == 2 and upa["columna"] == "VALOR"
    acciones = [f for f in filas if f["formato"] == "211" and f["columna"] == "NÚMERO DE ACCIONES"][0]
    assert acciones["valor"] == 41116694690.0 and acciones["grupo"] == "semestrales"


class PaginaFalsa:
    pedidos: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def informe(self, tipo, codigo, corte, nombre=""):
        PaginaFalsa.pedidos.append((tipo, codigo, corte))
        if corte == date(2025, 12, 31) and (tipo, codigo) == (260, 36):
            return {"anexos": sfc_niif.filas_anexos(ANEXOS, False), "xbrl": (NOMBRE, MUESTRA, "2026-03-25")}
        return {"anexos": [], "xbrl": None}


def test_conector_carga_y_no_repite(carpeta_datos, monkeypatch):
    monkeypatch.setattr(sfc_niif, "emisores", lambda: [
        {"emisor_id": "CO-COC04", "nit": "8999990681", "nombre": "Ecopetrol", "codigos": [{"tipo": 260, "codigo": 36}]}])
    monkeypatch.setattr(sfc_niif, "INICIO", date(2025, 9, 30))
    monkeypatch.setattr(sfc_niif, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 3, 30))}))
    PaginaFalsa.pedidos = []
    con = sfc_niif.ConectorNIIF(pagina_factory=PaginaFalsa)
    res = con.ejecutar()
    assert res.estado == "ok", res.detalle
    xbrl = storage.leer_tabla("silver", "fact_xbrl_sfc")
    fila = xbrl.filter((pl.col("concepto") == "ifrs:Assets") & (pl.col("fecha_fin") == date(2025, 12, 31))).row(0, named=True)
    assert fila["fecha_corte"] == date(2025, 12, 31) and fila["tipo_informe"] == "consolidado"
    assert fila["fecha_transmision"] == date(2026, 3, 25) and fila["entidad_sfc"] == "260-36"
    anexos = storage.leer_tabla("silver", "fact_anexo_sfc")
    assert anexos.filter(pl.col("concepto") == "Utilidad por acción").get_column("valor").to_list() == [1765.09]
    assert sorted(c for _, _, c in PaginaFalsa.pedidos) == [date(2025, 9, 30), date(2025, 12, 31)]
    # Segunda corrida: dic-2025 ya está y sep-2025 es reciente sin nada, así que solo se reintenta ese
    PaginaFalsa.pedidos = []
    sfc_niif.ConectorNIIF(pagina_factory=PaginaFalsa).ejecutar()
    assert [c for _, _, c in PaginaFalsa.pedidos] == [date(2025, 9, 30)]


def test_escala_segun_concepto_y_unidad():
    assert sfc_niif._escala("BasicEarningsLossPerShare", "peso") == "pesos"
    assert sfc_niif._escala("DividendosDecretados", "PesoxAccion") == "pesos"
    assert sfc_niif._escala("Assets", "COP") == "miles_de_pesos"
    assert sfc_niif._escala("NumberOfSharesOutstanding", "shares") == "unidades"


class PaginaXBRLRoto(PaginaFalsa):
    def informe(self, tipo, codigo, corte, nombre=""):
        PaginaFalsa.pedidos.append((tipo, codigo, corte))
        if corte == date(2025, 12, 31):
            return {"anexos": sfc_niif.filas_anexos(ANEXOS, False), "xbrl": (NOMBRE, b"<xbrl><sin cerrar", "2026-03-25")}
        return {"anexos": [], "xbrl": None}


def test_xbrl_ilegible_guarda_anexos_y_se_reintenta_tres_veces(carpeta_datos, monkeypatch):
    monkeypatch.setattr(sfc_niif, "emisores", lambda: [
        {"emisor_id": "CO-COC04", "nit": "8999990681", "nombre": "Ecopetrol", "codigos": [{"tipo": 260, "codigo": 36}]}])
    monkeypatch.setattr(sfc_niif, "INICIO", date(2025, 12, 31))
    monkeypatch.setattr(sfc_niif, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 9, 30))}))
    pedidos_por_corrida = []
    for _ in range(4):
        PaginaFalsa.pedidos = []
        res = sfc_niif.ConectorNIIF(pagina_factory=PaginaXBRLRoto).ejecutar()
        pedidos_por_corrida.append(sum(c == date(2025, 12, 31) for _, _, c in PaginaFalsa.pedidos))
    # Los anexos quedan guardados desde la primera corrida
    assert storage.leer_tabla("silver", "fact_anexo_sfc").height > 0
    # Se vuelve a pedir el corte hasta completar 3 intentos y luego se deja
    assert pedidos_por_corrida == [1, 1, 1, 0]


def test_anexos_con_textos_vacios():
    # Celdas, columnas o formatos sin texto (None) no rompen la lectura
    respuesta = {"trimestrales": {"formatos": [{"codigoFormato": "110000", "nombre": None}],
                                  "columnasFormatos": [{"codigoFormato": "110000", "codigoColumna": 1, "nombreColumna": None}],
                                  "detalleFormatos": [{"codigoFormato": "110000", "numColumna": 0, "valorColumna": None},
                                                      {"codigoFormato": "110000", "numColumna": 1, "valorColumna": None}]}}
    filas = sfc_niif.filas_anexos(respuesta, True)
    assert filas == [{"grupo": "trimestrales", "consolidado": True, "formato": "110000", "nombre_formato": "", "fila": 1,
                      "concepto": "", "columna": "", "valor_texto": "", "valor": None}]


def test_error_del_servidor_no_es_un_xbrl():
    error_500 = b'{"timestamp":"2026-11-24T04:45:19.785+00:00","status":500,"error":"Internal Server Error","path":"/descarga"}'
    assert not sfc_niif.es_xbrl(200, error_500)
    assert not sfc_niif.es_xbrl(500, b"<html>error</html>")
    assert sfc_niif.es_xbrl(200, b'<?xml version="1.0"?><xbrli:xbrl/>')
    assert sfc_niif.es_xbrl(200, b'\xef\xbb\xbf<?xml version="1.0"?>')
