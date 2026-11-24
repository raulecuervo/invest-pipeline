from datetime import date, datetime, timezone

import polars as pl
import pytest

from invest import distribuciones, retornos, storage

AHORA = datetime(2026, 11, 22, tzinfo=timezone.utc)


def _precios(filas):
    return pl.DataFrame(
        [{"fecha": f, "especie": e, "codigo_titulo": e.upper(), "precio_promedio": p, "valor_transado": v,
          "fuente_id": "sfc_precios_promedio", "ingest_ts": AHORA} for e, f, p, v in filas]
    )


def test_descarta_dias_sin_negociacion():
    d = date
    df = _precios([
        ("a", d(2025, 7, 1), 100.0, 1e6), ("a", d(2025, 7, 2), 100.0, 0.0),   # precio repetido: fuera
        ("a", d(2025, 7, 3), 101.0, 0.0),                                    # cambió con monto ínfimo: queda
        ("a", d(2025, 7, 4), 101.0, 5e5), ("b", d(2025, 7, 2), 50.0, 0.0),   # primera fila de b: queda
    ])
    quedan = distribuciones.negociados(df)
    assert quedan.filter(pl.col("especie") == "a").get_column("fecha").to_list() == [
        d(2025, 7, 1), d(2025, 7, 3), d(2025, 7, 4)]
    assert quedan.filter(pl.col("especie") == "b").height == 1


@pytest.fixture
def escision(carpeta_datos, monkeypatch):
    d = date
    filas = [
        # Holding: suspendida del 21 al 25 (la SFC repite el precio con monto 0) y reanuda el 28
        ("hold", d(2025, 7, 18), 24000.0, 1e9), ("hold", d(2025, 7, 21), 24000.0, 0.0),
        ("hold", d(2025, 7, 25), 24000.0, 0.0), ("hold", d(2025, 7, 28), 11000.0, 1e9),
        ("hold", d(2025, 7, 29), 11100.0, 1e9),
        # Acción recibida: negocia desde el 28
        ("recib", d(2025, 7, 18), 50000.0, 1e9), ("recib", d(2025, 7, 28), 54000.0, 1e9),
        # Otra holding cuya acción recibida debuta tarde: se usa el precio de referencia
        ("otra", d(2022, 3, 18), 1000.0, 1e9), ("otra", d(2022, 3, 31), 800.0, 1e9),
        ("nueva", d(2022, 4, 19), 286.0, 1e9),
    ]
    storage.upsert_por_anio("silver", "fact_precio_sfc", _precios(filas), ("fecha", "especie"))
    storage.escribir_completa("gold", "dim_activo", pl.DataFrame({
        "especie": ["hold", "recib", "otra", "nueva"], "emisor_id": ["H", "R", "O", "N"],
        "clase_accion": ["ordinaria"] * 4}))
    monkeypatch.setattr(distribuciones, "config", lambda: {"ventana_precio_dias": 10, "eventos": [
        {"especie": "hold", "fecha_ex": "2025-07-28", "tipo": "escision", "fuente": "prueba",
         "entrega": [{"especie": "recib", "nombre": "Recibida", "acciones_por_accion": 0.25}]},
        {"especie": "otra", "fecha_ex": "2022-03-30", "tipo": "escision", "fuente": "prueba",
         "entrega": [{"especie": "nueva", "nombre": "Nueva", "acciones_por_accion": 1, "precio_referencia": 291}]},
    ]})
    return carpeta_datos


def test_escision_suma_lo_entregado_al_retorno_total(escision):
    tabla, resumen = distribuciones.construir()
    assert resumen == {"eventos": 2, "precio_mercado": 1, "precio_referencia": 1, "sin_precio": 0, "filas": 2}
    hold = tabla.filter(pl.col("especie") == "hold").row(0, named=True)
    assert hold["valor"] == pytest.approx(0.25 * 54000) and hold["fecha_precio"] == date(2025, 7, 28)
    otra = tabla.filter(pl.col("especie") == "otra").row(0, named=True)
    assert otra["origen_precio"] == "referencia" and otra["valor"] == 291

    df, res = retornos.construir()
    h = df.filter(pl.col("especie") == "hold").sort("fecha")
    # Los días sin negociación de la suspensión no son filas: el 28 se compara con el 18
    assert h.get_column("fecha").to_list() == [date(2025, 7, 18), date(2025, 7, 28), date(2025, 7, 29)]
    dia = h.filter(pl.col("fecha") == date(2025, 7, 28)).row(0, named=True)
    assert dia["dias_desde_anterior"] == 10
    assert dia["retorno_precio"] == pytest.approx(11000 / 24000 - 1)
    assert dia["retorno_total"] == pytest.approx((11000 + 13500) / 24000 - 1)
    assert dia["distribucion"] == pytest.approx(13500)
    assert dia["factor_ajuste"] == pytest.approx(11000 / 24500)
    assert not dia["salto_atipico"]  # cae 54 %, pero lo explica la escisión
    o = df.filter((pl.col("especie") == "otra") & (pl.col("fecha") == date(2022, 3, 31))).row(0, named=True)
    assert o["retorno_total"] == pytest.approx((800 + 291) / 1000 - 1)
    assert res["distribuciones_aplicadas"] == 2
    assert res["dias_sin_negociacion_descartados"] == 2


def test_eventos_reales_bien_formados():
    """La lista curada: cada evento con fuente, relación positiva y precio para la acción fuera de la SFC."""
    cfg = distribuciones.config()
    assert len(cfg["eventos"]) >= 10
    for ev in cfg["eventos"]:
        assert ev["fuente"] and ev["tipo"] in ("escision", "reparto_acciones")
        date.fromisoformat(str(ev["fecha_ex"]))
        for ent in ev["entrega"]:
            assert ent["acciones_por_accion"] > 0
            if not ent.get("especie"):
                assert ent.get("precio_referencia")


def test_liquidez_diaria(carpeta_datos):
    from datetime import timedelta

    from invest import liquidez

    inicio = date(2026, 1, 5)
    filas = []
    for i in range(25):
        d = inicio + timedelta(days=i)
        filas.append(("liq", d, 100.0, 3e8))                          # negocia todos los días
        filas.append(("poco", d, 50.0, 1e9 if i % 4 == 0 else 0.0))   # monto alto, pero 1 de cada 4 días
    storage.upsert_por_anio("silver", "fact_precio_sfc", _precios(filas), ("fecha", "especie"))
    df, resumen = liquidez.construir()
    ult = {r["especie"]: r for r in df.filter(pl.col("fecha") == inicio + timedelta(days=24)).iter_rows(named=True)}
    assert ult["liq"]["liquida"] and ult["liq"]["fraccion_dias_20"] == 1.0
    assert ult["poco"]["monto_medio_20"] == pytest.approx(5 * 1e9 / 20)  # supera el monto…
    assert not ult["poco"]["liquida"]                                     # …pero negocia 25 % de los días
    assert df.filter(pl.col("fecha") == inicio).get_column("monto_medio_20").null_count() == 2  # sin ventana
    assert resumen == {"acciones": 2, "liquidas_hoy": 1}
