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
        {"cartera_id": "c", "fecha_operacion": "2025-01-02", "tipo": "compra", "especie": "coa01paao001", "cantidad": "5",
         "precio": "100", "moneda": "COP"}]}).json()
    assert r["ok"] and usuario.leer("fact_transaccion").height == 2
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
