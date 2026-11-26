"""Salud de datos: un fallo pasajero con el dato al día es un aviso; con el dato vencido, una falla."""

from datetime import date

import polars as pl

from invest import cli, salud
from invest.ingest import socrata
from invest.ingest.trm import ConectorTRM


def _trm(carpeta_datos, monkeypatch, registros):
    monkeypatch.setattr(socrata, "consultar", lambda *a, **k: registros)
    assert ConectorTRM().ejecutar(date(2026, 11, 14), date(2026, 11, 23)).estado == "ok"

    def falla(*a, **k):
        raise socrata.ErrorSocrata("sin conexión")

    monkeypatch.setattr(socrata, "consultar", falla)
    assert ConectorTRM().ejecutar(date(2026, 11, 14), date(2026, 11, 23)).estado == "error"


def test_fallo_pasajero_con_dato_al_dia_es_aviso(carpeta_datos, registros_trm, monkeypatch, capsys):
    _trm(carpeta_datos, monkeypatch, registros_trm)
    fila = salud.evaluar(hoy=date(2026, 11, 23)).filter(pl.col("fuente_id") == "trm_datos_gov").row(0, named=True)
    assert fila["estado"] == "al día (falló la última corrida)"
    # Semanas después, sin dato nuevo, ya está vencido
    fila = salud.evaluar(hoy=date(2026, 12, 18)).filter(pl.col("fuente_id") == "trm_datos_gov").row(0, named=True)
    assert fila["estado"] == "vencido"


def test_el_aviso_no_marca_la_corrida_en_rojo(carpeta_datos, registros_trm, monkeypatch, capsys):
    _trm(carpeta_datos, monkeypatch, registros_trm)
    original = salud.evaluar
    monkeypatch.setattr(salud, "evaluar", lambda hoy=None: _solo_trm(original(date(2026, 11, 23))))
    assert cli.main(["salud", "--solo", "actions"]) == 0
    salida = capsys.readouterr().out
    assert "falló la última corrida" in salida and "trm_datos_gov" in salida


def _solo_trm(df):
    return df.filter(pl.col("fuente_id") == "trm_datos_gov")


def test_fuente_manual_sin_leer_no_es_falla(carpeta_datos):
    fila = salud.evaluar(hoy=date(2026, 11, 25)).filter(pl.col("fuente_id") == "bvc_en_curso").row(0, named=True)
    assert fila["estado"] == "manual, sin leer todavía"
