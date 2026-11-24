from datetime import date

import polars as pl

from invest import salud, storage
from invest.calendario import dia_habil_anterior, es_habil
from invest.ingest import socrata
from invest.ingest.trm import ConectorTRM, huecos_calendario


def test_festivo_ley_emiliani():
    # 11 de noviembre de 2026 (Independencia de Cartagena) se traslada al lunes 16.
    assert not es_habil(date(2026, 11, 16), "XBOG")
    assert es_habil(date(2026, 11, 17), "XBOG")
    assert dia_habil_anterior(date(2026, 11, 17), "XBOG") == date(2026, 11, 13)


def test_interpretar_expande_vigencias(registros_trm):
    df = ConectorTRM().interpretar(registros_trm)
    assert df.height == 10  # del 14 al 23 de noviembre, un día por fila
    assert huecos_calendario(df) == []
    domingo = df.filter(pl.col("fecha") == date(2026, 11, 22)).row(0, named=True)
    assert domingo["tasa"] == 3461.92
    assert domingo["vigencia_desde"] == date(2026, 11, 21)
    # La TRM que rige desde el sábado 21 se calculó con las operaciones del viernes 20.
    assert domingo["fecha_mercado"] == date(2026, 11, 20)
    # La que rige desde el sábado 14 (sábado, domingo, lunes festivo y martes) viene del viernes 13.
    martes = df.filter(pl.col("fecha") == date(2026, 11, 17)).row(0, named=True)
    assert martes["tasa"] == 3316.7
    assert martes["fecha_mercado"] == date(2026, 11, 13)


def test_validar_envia_anomalias_a_cuarentena(registros_trm):
    malos = registros_trm + [
        {"valor": "-1", "unidad": "COP", "vigenciadesde": "2026-11-24T00:00:00.000",
         "vigenciahasta": "2026-11-24T00:00:00.000"},
    ]
    conector = ConectorTRM()
    validas, cuarentena = conector.validar(conector.interpretar(malos))
    assert validas.height == 10
    assert cuarentena.height == 1
    assert cuarentena.get_column("motivo")[0] == "tasa no positiva"


def _dia(fecha: str, valor: str) -> dict:
    return {"valor": valor, "unidad": "COP", "vigenciadesde": f"{fecha}T00:00:00.000",
            "vigenciahasta": f"{fecha}T00:00:00.000"}


def test_pico_aislado_va_a_cuarentena(registros_trm):
    # 24-nov sube 12,7 % y el 25-nov vuelve: parece un error de digitación
    con_pico = registros_trm + [_dia("2026-11-24", "3900"), _dia("2026-11-25", "3465")]
    conector = ConectorTRM()
    _, cuarentena = conector.validar(conector.interpretar(con_pico))
    assert cuarentena.get_column("fecha").to_list() == [date(2026, 11, 24)]
    assert "pico aislado" in cuarentena.get_column("motivo")[0]


def test_movimiento_grande_sostenido_se_conserva(registros_trm):
    # Como el 10 de marzo de 2020: sube 6 % y se queda arriba
    sostenido = registros_trm + [_dia("2026-11-24", "3670"), _dia("2026-11-25", "3690")]
    conector = ConectorTRM()
    validas, cuarentena = conector.validar(conector.interpretar(sostenido))
    assert cuarentena.is_empty() and validas.filter(pl.col("tasa") == 3670).height == 1


def test_variacion_extrema_va_a_cuarentena(registros_trm):
    extremo = registros_trm + [_dia("2026-11-24", "4100")]  # +18 %
    conector = ConectorTRM()
    _, cuarentena = conector.validar(conector.interpretar(extremo))
    assert cuarentena.height == 1 and "mayor a 15%" in cuarentena.get_column("motivo")[0]


def test_ejecucion_completa_e_idempotente(carpeta_datos, registros_trm, monkeypatch):
    monkeypatch.setattr(socrata, "consultar", lambda *a, **k: registros_trm)
    conector = ConectorTRM()
    r1 = conector.ejecutar(date(2026, 11, 14), date(2026, 11, 23))
    assert r1.estado == "ok", r1.detalle
    assert r1.archivo_bronze.exists()
    r2 = ConectorTRM().ejecutar(date(2026, 11, 14), date(2026, 11, 23))
    assert r2.estado == "ok"
    silver = storage.leer_tabla("silver", "fact_fx_diaria")
    assert silver.height == 10  # correr dos veces no duplica filas
    assert (carpeta_datos / "silver" / "fact_fx_diaria" / "anio=2026" / "parte.parquet").exists()
    assert len(storage.leer_ejecuciones()) == 2

    reporte = salud.evaluar(hoy=date(2026, 11, 23))
    fila = reporte.filter(pl.col("fuente_id") == "trm_datos_gov").row(0, named=True)
    assert fila["ultimo_dato"] == date(2026, 11, 20)
    assert fila["estado"] == "al día"


def test_error_de_red_queda_en_bitacora(carpeta_datos, monkeypatch):
    def falla(*a, **k):
        raise socrata.ErrorSocrata("sin conexión")

    monkeypatch.setattr(socrata, "consultar", falla)
    r = ConectorTRM().ejecutar(date(2026, 11, 14), date(2026, 11, 23))
    assert r.estado == "error"
    assert storage.leer_ejecuciones()[-1]["estado"] == "error"
