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
    return TestClient(servidor.app)


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
