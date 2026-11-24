from datetime import date, datetime, timezone

import polars as pl
import pytest

from invest import retornos, storage

AHORA = datetime(2026, 11, 22, tzinfo=timezone.utc)


def _precios(filas):
    return pl.DataFrame(
        [{"fecha": f, "especie": e, "codigo_titulo": e.upper(), "precio_promedio": p, "valor_transado": 1e9,
          "fuente_id": "sfc_precios_promedio", "ingest_ts": AHORA} for e, f, p in filas]
    )


def _dividendo(**kw):
    base = {"emisor_id": "CO-COTK7", "ejercicio": 2025, "clase_accion": "ordinaria", "tipo": "ordinario",
            "forma": "efectivo", "cuota": 1, "moneda": "COP", "valor_por_accion": None, "valor_cuota": 5.0,
            "fecha_ex_dividendo": None, "fecha_pago": None, "solo_propuesto": False, "revisado": True,
            "conflicto_entre_documentos": False, "eventos": "e1", "documentos": 1}
    return {**base, **kw}


@pytest.fixture
def mercado(carpeta_datos):
    d = date
    filas = [
        ("cotk7paao003", d(2026, 3, 20), 100.0), ("cotk7paao003", d(2026, 3, 24), 102.0),  # 23-mar festivo
        ("cotk7paao003", d(2026, 3, 25), 95.0), ("cotk7paao003", d(2026, 3, 26), 96.0),
        ("cotk7paad014", d(2026, 3, 20), 80.0), ("cotk7paad014", d(2026, 3, 26), 76.0),  # poco líquida
        ("cotk7paao003", d(2025, 12, 30), 90.0),
    ]
    storage.upsert_por_anio("silver", "fact_precio_sfc", _precios(filas), ("fecha", "especie"))
    storage.escribir_completa("gold", "dim_activo", pl.DataFrame({
        "especie": ["cotk7paao003", "cotk7paad014"], "emisor_id": ["CO-COTK7", "CO-COTK7"],
        "clase_accion": ["ordinaria", "preferencial"]}))
    storage.upsert_por_anio("silver", "fact_fx_diaria", pl.DataFrame([{
        "fecha": d(2026, 3, 25), "moneda_origen": "USD", "moneda_destino": "COP", "tasa": 4000.0, "tipo": "TRM",
        "vigencia_desde": d(2026, 3, 25), "vigencia_hasta": d(2026, 3, 25), "fecha_mercado": d(2026, 3, 24),
        "fuente_id": "trm_datos_gov", "ingest_ts": AHORA}]), ("fecha", "moneda_origen", "moneda_destino", "tipo"))
    return carpeta_datos


def _gold_dividendos(filas):
    storage.escribir_completa("gold", "fact_dividendo", pl.DataFrame(filas))


def test_regla_del_periodo_ex_dividendo():
    # Pago el miércoles 15-abr-2026: el periodo ex empieza 4 días hábiles antes, el jueves 9
    assert retornos.estimar_fecha_ex(date(2026, 4, 15)) == date(2026, 4, 9)
    # Pago en domingo: cuenta el lunes 20 como día de pago
    assert retornos.estimar_fecha_ex(date(2026, 4, 19)) == date(2026, 4, 14)
    assert retornos.dias_periodo_ex(date(2010, 4, 15)) == 10 and retornos.dias_periodo_ex(date(2026, 4, 15)) == 4


def test_retorno_total_con_dividendo(mercado):
    _gold_dividendos([_dividendo(fecha_ex_dividendo=date(2026, 3, 25), valor_cuota=5.0)])
    df, resumen = retornos.construir()
    o = df.filter(pl.col("especie") == "cotk7paao003").sort("fecha")
    dia = o.filter(pl.col("fecha") == date(2026, 3, 25)).row(0, named=True)
    assert dia["dividendo"] == 5.0
    assert dia["retorno_precio"] == pytest.approx(95 / 102 - 1)
    assert dia["retorno_total"] == pytest.approx(100 / 102 - 1)  # el precio cae lo que se pagó
    assert dia["factor_ajuste"] == pytest.approx(95 / 100)
    assert o.filter(pl.col("fecha") == date(2026, 3, 24)).row(0, named=True)["dias_desde_anterior"] == 4
    # La preferencial no recibe un dividendo de la clase ordinaria
    assert df.filter(pl.col("especie") == "cotk7paad014").get_column("dividendo").sum() == 0
    assert resumen["dividendos_aplicados"] == 1

    ajustado = retornos.precio_ajustado(o)
    assert ajustado.get_column("precio_ajustado")[-1] == pytest.approx(96.0)  # el último día es el precio real
    antes = ajustado.filter(pl.col("fecha") == date(2026, 3, 24)).row(0, named=True)
    assert antes["precio_ajustado"] == pytest.approx(102 * 95 / 100)  # reinversión al precio del día ex
    # Los retornos del precio ajustado son exactamente los retornos totales
    r = ajustado.with_columns((pl.col("precio_ajustado") / pl.col("precio_ajustado").shift(1) - 1).alias("r"))
    assert r.select((pl.col("r") - pl.col("retorno_total")).abs().max()).item() == pytest.approx(0, abs=1e-12)


def test_dividendo_en_dia_sin_negociacion_y_fecha_estimada(mercado):
    _gold_dividendos([
        # "todas": aplica a ordinaria y preferencial; la preferencial no negoció el 25, va al 26
        _dividendo(clase_accion="todas", fecha_ex_dividendo=date(2026, 3, 25), valor_cuota=2.0),
        # Sin fecha ex: pago lunes 30-mar -> periodo ex desde el martes 24 (lunes 23 festivo)
        _dividendo(fecha_pago=date(2026, 3, 30), valor_cuota=1.0, cuota=2),
    ])
    df, _ = retornos.construir()
    pref = df.filter((pl.col("especie") == "cotk7paad014") & (pl.col("fecha") == date(2026, 3, 26))).row(0, named=True)
    assert pref["dividendo"] == 2.0 and pref["retorno_total"] == pytest.approx(78 / 80 - 1)
    ordi = df.filter((pl.col("especie") == "cotk7paao003") & (pl.col("fecha") == date(2026, 3, 24))).row(0, named=True)
    assert ordi["dividendo"] == 1.0 and ordi["fecha_ex_estimada"] is True


def test_conversion_de_dolares_y_exclusiones(mercado):
    _gold_dividendos([
        _dividendo(moneda="USD", valor_cuota=0.001, fecha_ex_dividendo=date(2026, 3, 25)),  # 4 pesos
        _dividendo(forma="acciones", valor_cuota=0.1, fecha_ex_dividendo=date(2026, 3, 26), cuota=2),
        _dividendo(solo_propuesto=True, valor_cuota=9.0, fecha_ex_dividendo=date(2026, 3, 26), cuota=3),
    ])
    df, resumen = retornos.construir()
    o = df.filter(pl.col("especie") == "cotk7paao003")
    assert o.filter(pl.col("fecha") == date(2026, 3, 25)).get_column("dividendo")[0] == pytest.approx(4.0)
    assert o.filter(pl.col("fecha") == date(2026, 3, 26)).get_column("dividendo")[0] == 0
    assert resumen["en_acciones"] == 1 and resumen["solo_propuestos"] == 1


def test_dividendo_absurdo_no_se_aplica(mercado):
    _gold_dividendos([_dividendo(fecha_ex_dividendo=date(2026, 3, 25), valor_cuota=500.0)])
    df, resumen = retornos.construir()
    dia = df.filter((pl.col("especie") == "cotk7paao003") & (pl.col("fecha") == date(2026, 3, 25))).row(0, named=True)
    assert dia["dividendo_invalido"] is True and dia["dividendo"] == 0 and resumen["dividendos_invalidos"] == 1


def test_sin_dividendos_el_retorno_total_es_el_de_precio(mercado):
    df, resumen = retornos.construir()
    assert resumen["dividendos_aplicados"] == 0
    assert df.filter(pl.col("retorno_total") != pl.col("retorno_precio")).is_empty()


def test_solo_se_reescriben_los_anios_que_cambian(mercado):
    carpeta = mercado / "gold" / "fact_retorno_diario"
    (carpeta).mkdir(parents=True, exist_ok=True)
    (carpeta / "fact_retorno_diario.parquet").write_bytes(b"version vieja sin particiones")
    retornos.construir()
    assert not (carpeta / "fact_retorno_diario.parquet").exists()
    anio_2025 = carpeta / "anio=2025" / "parte.parquet"
    antes = anio_2025.stat().st_mtime_ns
    # Entra un dividendo de 2026: 2025 no cambia y no se toca
    _gold_dividendos([_dividendo(fecha_ex_dividendo=date(2026, 3, 25), valor_cuota=5.0)])
    escritos = storage.escribir_por_anio("gold", "fact_retorno_diario", retornos.construir()[0], "fecha")
    assert escritos == [] and anio_2025.stat().st_mtime_ns == antes


def test_salto_atipico_se_marca(carpeta_datos):
    # Como la BVC en mayo de 2018: el precio se multiplica por cientos de un día a otro
    storage.upsert_por_anio("silver", "fact_precio_sfc", _precios([
        ("cor01paao011", date(2018, 4, 27), 22.5), ("cor01paao011", date(2018, 5, 2), 11188.7),
        ("cor01paao011", date(2018, 5, 3), 11200.0)]), ("fecha", "especie"))
    df, resumen = retornos.construir()
    assert df.get_column("salto_atipico").to_list() == [False, True, False] and resumen["saltos_atipicos"] == 1
