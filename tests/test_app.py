from datetime import date

import polars as pl
import pytest

from invest import storage, usuario

fastapi = pytest.importorskip("fastapi")


@pytest.fixture
def cliente(carpeta_datos):
    from fastapi.testclient import TestClient

    from invest.app import servidor

    (carpeta_datos / "gold" / "dims").mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"especie": ["coa01paao001"], "nemotecnico": ["AAA"], "nombre": ["AO A S.A."], "emisor_id": ["CO-COA01"],
                  "clase_accion": ["ordinaria"], "activa": [True], "clase_activo_id": ["ACCION"], "pais_id": ["CO"], "moneda_id": ["COP"]}).write_parquet(carpeta_datos / "gold" / "dims" / "dim_activo.parquet")
    pl.DataFrame({"emisor_id": ["CO-COA01"], "nombre_corto": ["A"], "sector_sfc": ["X"]}).write_parquet(
        carpeta_datos / "gold" / "dims" / "dim_emisor.parquet")
    storage.escribir_por_anio("gold", "fact_precio_diario", pl.DataFrame(
        [{"fecha": date(2025, 1, 2), "especie": "coa01paao001", "precio": 100.0, "precio_promedio": 100.0}]), "fecha")
    return TestClient(servidor.app, base_url="http://127.0.0.1:8765", headers={"X-Invest": "1"})


def test_paginas_y_estado(cliente):
    assert cliente.get("/").status_code == 200
    e = cliente.get("/api/estado").json()
    assert e["ultimo_precio"] == "2025-01-02" and e["carteras"] == []
    # Tablas que aún no existen no rompen las páginas
    assert cliente.get("/api/matriz").status_code == 200
    assert cliente.get("/api/activos").json()[0]["nemotecnico"] == "AAA"


def test_guardar_valida_y_recalcula(cliente):
    r = cliente.post("/api/usuario/dim_cartera", json={"filas": [{"cartera_id": "c", "nombre": "C", "tipo": "trueque", "moneda_base": "COP"}]}).json()
    assert not r["ok"] and any("no permitidos" in p for p in r["problemas"])
    r = cliente.post("/api/usuario/dim_cartera", json={"filas": [{"cartera_id": "c", "nombre": "C", "tipo": "real", "moneda_base": "COP"}]}).json()
    assert r["ok"]
    r = cliente.post("/api/usuario/fact_transaccion", json={"filas": [
        {"cartera_id": "c", "fecha_operacion": "2025-01-02", "tipo": "aporte", "monto": "1000", "moneda": "COP"},
        {"cartera_id": "c", "fecha_operacion": "2025-01-02", "tipo": "compra", "especie": "aaa", "cantidad": "5",
         "precio": "100", "moneda": "COP"}]}).json()
    assert r["ok"] and usuario.leer("fact_transaccion").height == 2
    # El nemotécnico escrito en el formulario se guarda como la especie de la SFC
    assert "coa01paao001" in usuario.leer("fact_transaccion").get_column("especie").to_list()
    tid = usuario.leer("fact_transaccion").get_column("transaccion_id")[0]
    assert cliente.post("/api/usuario/fact_transaccion", json={"borrar": [{"transaccion_id": tid}]}).json()["filas"] == 1
    assert cliente.get("/api/estado").json()["carteras"][0]["cartera_id"] == "c"


def test_accion_desconocida(cliente):
    assert cliente.post("/api/acciones/borrar_todo").status_code == 404


def test_rechaza_pedidos_de_otros_sitios(cliente):
    from invest.app.servidor import motivo_rechazo

    # Cambio de DNS: el navegador manda el dominio del atacante en Host
    assert cliente.get("/api/estado", headers={"host": "malo.example:8765"}).status_code == 403
    # Formulario o fetch desde otra página: sin el encabezado propio, o con otro origen
    assert cliente.post("/api/acciones/actualizar_modelo", headers={"X-Invest": ""}).status_code == 403
    assert cliente.post("/api/acciones/actualizar_modelo", headers={"origin": "https://malo.example"}).status_code == 403
    assert cliente.get("/api/estado", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    # Lo que manda el propio aplicativo pasa
    assert motivo_rechazo("POST", {"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765", "x-invest": "1",
                                   "sec-fetch-site": "same-origin"}) is None
    assert motivo_rechazo("GET", {"host": "localhost:8765"}) is None
    assert motivo_rechazo("GET", {"host": "[::1]:8765"}) is None
    assert motivo_rechazo("GET", {"host": "127.0.0.1.malo.example"}) == "host no permitido"


def test_puerto_ocupado_por_otro_programa_usa_el_siguiente():
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from invest.app import servidor

    class Otro(BaseHTTPRequestHandler):
        def do_GET(self):  # otro aplicativo: responde, pero no es este
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>Matriz de seguimiento financiero</html>")

        def log_message(self, *a):
            pass

    with socket.socket() as s:  # un puerto libre para empezar
        s.bind(("127.0.0.1", 0))
        base = s.getsockname()[1]
    otro = HTTPServer(("127.0.0.1", base), Otro)
    threading.Thread(target=otro.serve_forever, daemon=True).start()
    try:
        puerto, ya_abierto = servidor.elegir_puerto(base, intentos=5)
        assert puerto != base and not ya_abierto
    finally:
        otro.shutdown()
        otro.server_close()


def test_reconoce_su_propia_identidad(cliente):
    assert cliente.get("/api/identidad").json() == {"app": "invest-pipeline"}


def test_ficha_acepta_nemotecnico_y_aviso_de_codigo_nuevo(cliente, monkeypatch):
    from invest.app import servidor

    assert servidor.resolver_especie("aaa") == "coa01paao001"
    assert servidor.resolver_especie("A") == "coa01paao001"
    assert servidor.resolver_especie("coa01paao001") == "coa01paao001"
    assert cliente.get("/api/ficha/AAA").json()["cabecera"]["especie"] == "coa01paao001"
    assert cliente.get("/api/estado").json()["reiniciar"] is False
    monkeypatch.setattr(servidor, "HUELLA_INICIO", "otra")
    assert cliente.get("/api/estado").json()["reiniciar"] is True


def test_precio_en_curso_hasta_que_llega_el_cierre(cliente):
    from datetime import datetime

    from invest.app import servidor
    from invest.ingest import bvc

    hoy = date(2025, 1, 3)
    fila = {c: None for c in bvc.ESQUEMA_EN_CURSO}
    fila.update({"fecha": hoy, "nemotecnico": "AAA", "especie": "coa01paao001", "cierre": 104.0, "variacion_pct": 4.0,
                 "volumen_cop": 5e8, "hora_lectura": datetime(2025, 1, 3, 11, 40), "estado_mercado": "Mercado abierto"})
    storage.upsert_por_anio("silver", "fact_precio_bvc_en_curso", pl.DataFrame([fila], schema=bvc.ESQUEMA_EN_CURSO),
                            ["fecha", "nemotecnico"])
    p = servidor.precios_en_curso(hoy)["coa01paao001"]
    assert p["precio"] == 104.0 and abs(p["variacion"] - 0.04) < 1e-12 and p["hora"] == "2025-01-03T11:40"
    assert servidor.precios_en_curso(date(2025, 1, 6)) == {}  # solo la lectura de hoy
    # Con el cierre del día en fact_precio_diario, el precio en curso deja de mostrarse
    storage.escribir_por_anio("gold", "fact_precio_diario", pl.DataFrame(
        [{"fecha": d, "especie": "coa01paao001", "precio": x, "precio_promedio": x}
         for d, x in ((date(2025, 1, 2), 100.0), (hoy, 103.0))]), "fecha")
    from invest import modelo
    modelo.construir_duckdb()
    assert servidor.precios_en_curso(hoy) == {}
    assert cliente.get("/api/en_curso").status_code == 200


def test_medias_moviles_del_precio():
    from invest.app import servidor
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert servidor._media_simple(xs, 3) == [None, None, 2.0, 3.0, 4.0]
    assert servidor._media_simple([1.0, None, 3.0, 4.0, 5.0], 2) == [None, None, None, 3.5, 4.5]
    e = servidor._media_exponencial(xs, 3)
    assert e[:2] == [None, None] and e[2] == 2.0 and e[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0) and e[4] == pytest.approx(0.5 * 5 + 0.5 * 3.0)


def test_revision_de_estados_guarda_la_decision_e_imagen(cliente, carpeta_datos):
    import csv

    from invest.app import servidor
    from invest.ingest import estados_compania as ec
    pytest.importorskip("pypdfium2")
    assert servidor.variantes_cifra("637979587.0") == ["637,979,587", "637.979.587"]
    assert servidor.variantes_cifra("2705.93") == ["2,705.93", "2.705,93"]
    # Un PDF de una página con la cifra impresa
    pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj "
           b"4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 40 100 Td (Total 958,581,540) Tj ET\nendstream endobj "
           b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")
    destino = carpeta_datos / "anexos" / "companias" / "CO-X" / "doc.pdf"
    destino.parent.mkdir(parents=True)
    destino.write_bytes(pdf)
    fila = {c: "" for c in ec.COLUMNAS_REVISION}
    fila.update(documento_id="d1", emisor_id="CO-X", concepto="pasivos", valor_a="637979587", valor_b="958581540",
                archivo="anexos/companias/CO-X/doc.pdf", pagina_b="1", unidades_a="miles")
    ec.ruta_revision().parent.mkdir(parents=True, exist_ok=True)
    with open(ec.ruta_revision(), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ec.COLUMNAS_REVISION, delimiter=";")
        w.writeheader(); w.writerow(fila)
    assert cliente.get("/api/revision_estados").json()[0]["concepto"] == "pasivos"
    img = cliente.get("/api/revision_estados/imagen", params={"archivo": fila["archivo"], "pagina": 1, "valores": "637979587,958581540"})
    assert img.status_code == 200 and img.content.startswith(b"\x89PNG")
    # Fuera de la carpeta de datos no se sirve nada
    assert cliente.get("/api/revision_estados/imagen", params={"archivo": "../../etc/passwd.pdf", "pagina": 1}).status_code == 400
    assert cliente.post("/api/revision_estados", json={"documento_id": "d1", "concepto": "pasivos", "revision": "corregido"}).status_code == 400
    r = cliente.post("/api/revision_estados", json={"documento_id": "d1", "concepto": "pasivos", "revision": "b"})
    assert r.status_code == 200 and ec.leer_revision()[("d1", "pasivos")]["revision"] == "b"


def test_documentos_por_categoria_activo_y_anio(cliente, carpeta_datos):
    import io
    import zipfile
    destino = carpeta_datos / "anexos" / "companias" / "CO-COA01" / "2025-12-31_estados.pdf"
    destino.parent.mkdir(parents=True)
    destino.write_bytes(b"%PDF-1.4 estados")
    zipeado = io.BytesIO()
    with zipfile.ZipFile(zipeado, "w") as z:
        z.writestr("EEFF Consolidados 2025.pdf", b"%PDF-1.4 " + b"x" * 5000)
    (carpeta_datos / "anexos" / "sfc" / "2026").mkdir(parents=True)
    (carpeta_datos / "anexos" / "sfc" / "2026" / "aaa_INFORME.zip").write_bytes(zipeado.getvalue())
    docs = {d["archivo"]: d for d in cliente.get("/api/documentos").json()}
    d = docs["anexos/companias/CO-COA01/2025-12-31_estados.pdf"]
    assert (d["categoria"], d["emisor"], d["anio"], d["origen"]) == ("Estados financieros", "A", 2025, "Sitio de la compañía")
    assert docs["anexos/sfc/2026/aaa_INFORME.zip"]["anio"] == 2026
    r = cliente.get("/api/documentos/archivo", params={"ruta": d["archivo"]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and "inline" in r.headers["content-disposition"]
    # Del ZIP se abre el PDF que trae adentro
    r = cliente.get("/api/documentos/archivo", params={"ruta": "anexos/sfc/2026/aaa_INFORME.zip"})
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    # Nada fuera de anexos/ ni fuentes_compania/
    for ruta in ("../../etc/passwd", "modelo.duckdb", "usuario/carteras.csv", "anexos/../modelo.duckdb"):
        assert cliente.get("/api/documentos/archivo", params={"ruta": ruta}).status_code == 400


def test_documentos_sin_leer_se_deciden_desde_el_aplicativo(cliente, carpeta_datos):
    from invest.ingest import estados_compania as ec
    fila = {c: "" for c in ec.COLUMNAS_SIN_LEER}
    ec.guardar_sin_leer([{**fila, "documento_id": "d1", "emisor_id": "CO-COT04", "nombre": "Grupo Nutresa",
                          "fecha_corte": "2025-03-31", "estado": "sin_paginas", "paginas_total": "40", "huella_pdf": "h"}])
    assert cliente.get("/api/revision_estados/sin_leer").json()[0]["estado"] == "sin_paginas"
    post = lambda **c: cliente.post("/api/revision_estados/sin_leer", json={"documento_id": "d1", **c}, headers={"X-Invest": "1"})
    assert post(decision="paginas", paginas="12 a 14, 15").json()["fila"]["paginas"] == "12-15"
    assert ec.decisiones_sin_leer()["d1"]["paginas"] == "12-15"
    assert post(decision="paginas", paginas="39-41").status_code == 400  # el documento tiene 40
    assert post(decision="paginas", paginas="doce").status_code == 400
    assert post(decision="borrar").status_code == 400
    assert post(decision="sin_estados").status_code == 200 and ec.decisiones_sin_leer()["d1"]["decision"] == "sin_estados"
    assert post(decision="").status_code == 200 and ec.decisiones_sin_leer() == {}
    assert cliente.post("/api/revision_estados/sin_leer", json={"documento_id": "otro", "decision": ""},
                        headers={"X-Invest": "1"}).status_code == 404


def test_laboratorio_api(cliente, carpeta_datos):
    d = cliente.get("/api/backtest/activos").json()
    assert {e["id"] for e in d["ejemplos"]} >= {"60_40", "efectivo"} and any(a["id"] == "efectivo_cop" for a in d["activos"])
    h = {"X-Invest": "1"}
    r = cliente.post("/api/backtest", json={"pesos": {"efectivo_cop": 50}}, headers=h)
    assert r.status_code == 400 and "suman" in r.json()["detail"]
    assert cliente.post("/api/backtest", json={"pesos": {"no_existe": 100}}, headers=h).status_code == 400
    # Escenarios guardados
    par = {"pesos": {"efectivo_cop": 100}, "rebalanceo": "anual"}
    assert cliente.post("/api/backtest/escenarios", json={"nombre": "Uno", "parametros": par}, headers=h).status_code == 200
    assert cliente.post("/api/backtest/escenarios", json={"nombre": "Uno", "parametros": par}, headers=h).json()["escenarios"][0]["nombre"] == "Uno"
    assert len(cliente.get("/api/backtest/escenarios").json()) == 1
    assert cliente.delete("/api/backtest/escenarios/Uno", headers=h).json()["escenarios"] == []
    assert cliente.delete("/api/backtest/escenarios/Uno", headers=h).status_code == 404
