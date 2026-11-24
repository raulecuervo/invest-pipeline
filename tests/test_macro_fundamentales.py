import io
import json
import zipfile
from datetime import date

import polars as pl
import pytest

from invest import salud, storage
from invest.ingest import cuif, dane_ipc, fred, sec_edgar, xlsx


def _xlsx(filas: list[list]) -> bytes:
    """Libro mínimo con textos compartidos, como los anexos del DANE."""
    textos: list[str] = []

    def celda(ref, v):
        if v is None:
            return ""
        if isinstance(v, str):
            if v not in textos:
                textos.append(v)
            return f'<c r="{ref}" t="s"><v>{textos.index(v)}</v></c>'
        return f'<c r="{ref}"><v>{v}</v></c>'

    letras = "ABCDEFGHIJ"
    cuerpo = "".join(
        f'<row r="{i + 1}">' + "".join(celda(f"{letras[j]}{i + 1}", v) for j, v in enumerate(f)) + "</row>"
        for i, f in enumerate(filas))
    m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {m} {r}><sheets><sheet name="IndicesIPC" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {m}><sheetData>{cuerpo}</sheetData></worksheet>")
        z.writestr("xl/sharedStrings.xml", f"<sst {m}>" + "".join(f"<si><t>{t}</t></si>" for t in textos) + "</sst>")
    return buf.getvalue()


ANEXO = [
    ["Total, Índice de Precios al Consumidor (IPC)"],
    ["Índices - Serie de empalme"],
    [None, None, None, "Base Diciembre de 2018 = 100,00"],
    ["Mes", 2024.0, 2025.0, 2026.0],
    ["Enero", 138.98, 146.24, 154.07],
    ["Febrero", 140.49, 147.9, 155.73],
    ["Octubre", 143.83, 151.76, 161.32],
    ["Noviembre", 144.22, 151.87, None],
    ["Fuente: DANE."],
    ["Actualizado el 9 de Noviembre de 2026"],
]


def test_lector_xlsx_y_anexo_ipc():
    contenido = _xlsx(ANEXO)
    assert xlsx.leer_hoja(contenido)[3] == ["Mes", 2024.0, 2025.0, 2026.0]
    registros, actualizado = dane_ipc.leer_indices(contenido)
    assert actualizado == date(2026, 11, 9)
    assert len(registros) == 11  # noviembre de 2026 aún no existe
    assert {"anio": 2026, "mes": 10, "valor": 161.32} in registros


def test_conector_ipc(carpeta_datos, monkeypatch):
    pagina = b'<a href="/files/operaciones/IPC/oct2026/anex-IPC-Indices-oct2026.xlsx">Indices</a>'
    pedidos = []

    def bajar(self, url, **kw):
        pedidos.append(url)
        return pagina if url.endswith("ipc") else _xlsx(ANEXO)

    monkeypatch.setattr(dane_ipc.ConectorDANEIPC, "bajar", bajar)
    res = dane_ipc.ConectorDANEIPC().ejecutar()
    assert res.estado == "ok", res.detalle
    assert pedidos[1] == "https://www.dane.gov.co/files/operaciones/IPC/oct2026/anex-IPC-Indices-oct2026.xlsx"
    df = storage.leer_tabla("silver", "fact_serie_macro").sort("fecha")
    assert df.height == 11 and set(df.get_column("serie_id")) == {"ipc_co"}
    ultimo = df.row(-1, named=True)
    assert ultimo["fecha"] == date(2026, 10, 1) and ultimo["fecha_publicacion"] == date(2026, 11, 9)
    assert not ultimo["publicacion_estimada"]
    ene = df.filter(pl.col("fecha") == date(2026, 1, 1)).row(0, named=True)
    assert ene["publicacion_estimada"] and ene["fecha_publicacion"] == date(2026, 2, 6)  # 5.º día hábil de feb


def test_fred(carpeta_datos, monkeypatch):
    csv = {"DGS10": b"observation_date,DGS10\n2026-11-18,5.01\n2026-11-19,.\n2026-11-20,5.04\n"}
    monkeypatch.setattr(fred.ConectorFRED, "bajar",
                        lambda self, url, **kw: csv.get(url.rsplit("=", 1)[-1]))  # el resto: sin cambios
    res = fred.ConectorFRED().ejecutar()
    assert res.estado == "ok", res.detalle
    df = storage.leer_tabla("silver", "fact_serie_macro")
    assert df.get_column("valor").to_list() == [5.01, 5.04]  # el "." de FRED se omite
    assert df.get_column("serie_id").unique().to_list() == ["fred_dgs10"]
    with pytest.raises(ValueError):
        fred.leer_csv(b"<html>", "X")


def _cuif(entidad, cuenta, valor, fecha="2026-09-30T00:00:00.000", signo="+"):
    return {"tipo_entidad": "1", "codigo_entidad": entidad, "nombre_entidad": f"BANCO {entidad}",
            "fecha_corte": fecha, "cuenta": cuenta, "nombre_cuenta": cuenta, "moneda": "0",
            "signo_valor": signo, "valor": str(valor)}


def test_cuif_cuadre_contable(carpeta_datos, monkeypatch):
    registros = [_cuif("7", "100000", 293898), _cuif("7", "200000", 266965), _cuif("7", "300000", 26933),
                 _cuif("7", "590000", 5932),
                 _cuif("1", "100000", 1000), _cuif("1", "200000", 500), _cuif("1", "300000", 100),  # no cuadra
                 _cuif("39", "100000", 10), _cuif("39", "200000", 12), _cuif("39", "300000", 2, signo="-")]
    vistos = {}

    def consultar(url, where=None, order=None, timeout=60, **kw):
        vistos["where"] = where
        return registros

    monkeypatch.setattr(cuif.socrata, "consultar", consultar)
    res = cuif.ConectorCUIF().ejecutar(date(2026, 9, 1), date(2026, 9, 30))
    assert "codigo_entidad in('1','2','7'" in vistos["where"] and "cuenta like '__0000'" in vistos["where"]
    assert res.estado == "revisar" and "no cuadra" in res.detalle
    df = storage.leer_tabla("silver", "fact_cuif_mensual")
    assert set(df.get_column("codigo_entidad")) == {"7", "39"}  # el patrimonio negativo de 39 cuadra
    assert df.filter((pl.col("codigo_entidad") == "39") & (pl.col("cuenta") == "300000")).item(0, "valor") == -2


COMPANYFACTS = {
    "cik": 1444406, "entityName": "ECOPETROL S.A.",
    "facts": {
        "ifrs-full": {
            "Assets": {"units": {"COP": [
                {"end": "2023-12-31", "val": 2.8e14, "accn": "a-24", "fy": 2023, "fp": "FY", "form": "20-F", "filed": "2024-04-20"},
                {"end": "2023-12-31", "val": 2.8e14, "accn": "a-25", "fy": 2024, "fp": "FY", "form": "20-F", "filed": "2025-04-23"},
                {"end": "2024-12-31", "val": 2.98e14, "accn": "a-25", "fy": 2024, "fp": "FY", "form": "20-F", "filed": "2025-04-23"}]}},
            "ProfitLoss": {"units": {"COP": [
                {"start": "2024-01-01", "end": "2024-12-31", "val": 1.85e13, "accn": "a-25", "fy": 2024, "fp": "FY",
                 "form": "20-F", "filed": "2025-04-23", "frame": "CY2024"}]}},
        },
        "us-gaap": {"Ignorado": {"units": {"USD": [{"end": "2024-12-31", "val": 1, "accn": "x"}]}}},
    },
}


def test_edgar(carpeta_datos, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert sec_edgar.ConectorEDGAR().ejecutar().estado == "error"  # sin correo de contacto no se consulta

    monkeypatch.setenv("SEC_USER_AGENT", "Prueba prueba@example.com")
    agentes = []

    def bajar(self, url, cabeceras=None, **kw):
        agentes.append(cabeceras["User-Agent"])
        return json.dumps(COMPANYFACTS).encode() if "1444406" in url else None

    monkeypatch.setattr(sec_edgar.ConectorEDGAR, "bajar", bajar)
    res = sec_edgar.ConectorEDGAR().ejecutar()
    assert res.estado == "ok", res.detalle
    assert set(agentes) == {"Prueba prueba@example.com"}
    df = storage.leer_tabla("silver", "fact_xbrl_sec")
    assert df.height == 4  # el comparativo de 2023 del 20-F siguiente se conserva; us-gaap no
    pl_ = df.filter(pl.col("concepto") == "ProfitLoss").row(0, named=True)
    assert pl_["tipo_periodo"] == "duracion" and pl_["fecha_inicio"] == date(2024, 1, 1)
    activo = df.filter((pl.col("concepto") == "Assets") & (pl.col("accn") == "a-24")).row(0, named=True)
    assert activo["tipo_periodo"] == "instante" and activo["fecha_presentacion"] == date(2024, 4, 20)
    # Repetir la corrida no duplica filas
    sec_edgar.ConectorEDGAR().ejecutar()
    assert storage.leer_tabla("silver", "fact_xbrl_sec").height == 4


def test_salud_tolera_segun_frecuencia():
    assert salud._tolerancia({"frecuencia": "diaria"}, 3) == 3
    assert salud._tolerancia({"frecuencia": "mensual"}, 3) == 50
    assert salud._tolerancia({"frecuencia": "anual", "tolerancia_dias_habiles": 400}, 3) == 400
