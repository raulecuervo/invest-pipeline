import csv
from datetime import date, datetime, timezone

import polars as pl
import pytest

from invest import dividendos_web, retornos, storage

AHORA = datetime(2026, 11, 23, tzinfo=timezone.utc)
d = date

CFG = {
    "desde": "2007-01-01", "rendimiento_maximo": 0.15, "veces_mediana": 8, "tolerancia_valor": 0.01,
    "dias_duplicado": 5, "dias_evento_antes": 450, "dias_evento_despues": 15,
    "fuentes": {"investing": {"carpeta": "fuentes_web/dividendos/investing", "ajustado_por_splits": True},
                "stockanalysis": {"carpeta": "fuentes_web/dividendos/stockanalysis", "ajustado_por_splits": True}},
    "activos": [
        {"especie": "cor01paao011", "investing": "bvc", "stockanalysis": "BVC"},
        {"especie": "cot13paao004", "investing": "gruposura", "stockanalysis": "GRUPOSURA",
         "excluir": [{"desde": "2010-01-01", "hasta": "2010-12-31", "motivo": "prueba de exclusión"}]},
    ],
}


def _escribir(base, fuente, nombre, lineas):
    carpeta = base / "fuentes_web" / "dividendos" / fuente
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / f"{nombre}.txt").write_text("\n".join(lineas) + "\n", encoding="utf-8")


@pytest.fixture
def datos(carpeta_datos, monkeypatch):
    monkeypatch.setattr(dividendos_web, "config", lambda: CFG)
    # BVC: valores publicados ajustados por la reagrupación 500:1 de 2018
    _escribir(carpeta_datos, "investing", "bvc", ["2017-04-24;225;I;2017-04-28", "2021-04-26;578.92;O;2021-04-30"])
    _escribir(carpeta_datos, "stockanalysis", "BVC", ["2021-04-26;578.920;2021-04-30"])
    # Sura: un duplicado de Investing, una escisión que stockanalysis no trae y un año excluido
    _escribir(carpeta_datos, "investing", "gruposura", [
        "2023-07-05;320;3M;2023-07-11", "2023-07-04;320;3M;2023-07-11", "2023-04-04;320;3M;2023-04-12",
        "2023-09-26;320;3M;2023-10-02", "2023-08-01;17940;O;2023-08-07", "2010-03-26;67;3M;2010-04-13"])
    _escribir(carpeta_datos, "stockanalysis", "GRUPOSURA", [
        "2023-04-04;320.000;2023-04-12", "2023-07-05;320.000;2023-07-11", "2023-09-26;320.000;2023-10-02"])
    storage.escribir_completa("gold", "fact_split", pl.DataFrame({
        "especie": ["cor01paao011"], "fecha": [d(2018, 5, 2)], "factor_acciones": [0.002],
        "origen": ["revisado"], "clasificacion": ["split_con_evento"]}))
    precios = []
    for f, p in [(d(2010, 3, 25), 30000.0), (d(2017, 4, 21), 22.5), (d(2017, 4, 24), 22.0), (d(2021, 4, 23), 11000.0),
                 (d(2021, 4, 26), 10500.0), (d(2023, 4, 3), 30000.0), (d(2023, 7, 4), 31000.0),
                 (d(2023, 7, 5), 30700.0), (d(2023, 7, 31), 31000.0), (d(2023, 9, 25), 32000.0)]:
        especie = "cor01paao011" if f.year in (2017, 2021) else "cot13paao004"
        precios.append({"fecha": f, "especie": especie, "codigo_titulo": especie.upper(), "precio_promedio": p,
                        "valor_transado": 1e9, "fuente_id": "sfc_precios_promedio", "ingest_ts": AHORA})
    storage.upsert_por_anio("silver", "fact_precio_sfc", pl.DataFrame(precios), ("fecha", "especie"))
    storage.escribir_completa("gold", "dim_activo", pl.DataFrame({
        "especie": ["cor01paao011", "cot13paao004"], "emisor_id": ["CO-COR01", "CO-COT13"],
        "nombre": ["AO BOLSA DE VALORES DE COLOMBIA S.A.", "AO GRUPO DE INVERSIONES SURAMERICANA S.A."],
        "clase_accion": ["ordinaria", "ordinaria"]}))
    eventos = [
        ("e1", d(2017, 3, 30), "CO-COR01", "Proyecto de distribución aprobado: dividendo de $0,45 por acción."),
        ("e2", d(2021, 3, 26), "CO-COR01", "Asamblea aprobó dividendos."),
        ("e3", d(2023, 3, 30), "CO-COT13", "Dividendo de $1.280 por acción pagadero en cuatro cuotas."),
        ("e4", d(2010, 3, 20), "CO-COT13", "Dividendo aprobado."),
    ]
    storage.escribir_por_anio("gold", "fact_evento_corporativo", pl.DataFrame([{
        "evento_id": i, "fecha": f, "hora": "17:00:00", "entidad": "", "tema": "Proyecto Utilidad",
        "resumen": r, "tiene_anexo": False, "anexo_archivo": None, "fuente_id": "sfc_informacion_relevante",
        "ingest_ts": AHORA, "categoria": "dividendo", "afecta_precio": False, "emisor_id": e}
        for i, f, e, r in eventos]), "fecha")
    return carpeta_datos


def _fila(tabla, especie, fecha):
    return tabla.filter((pl.col("especie") == especie) & (pl.col("fecha_ex") == fecha)).row(0, named=True)


def test_monto_texto():
    assert dividendos_web.monto_texto("1.250,50") == 1250.5
    assert dividendos_web.monto_texto("107") == 107
    assert dividendos_web.monto_texto("2.300") == 2300
    assert dividendos_web.monto_texto("0,45") == 0.45


def test_verifica_y_deshace_el_ajuste_por_split(datos):
    tabla, resumen = dividendos_web.detectar()
    bvc = _fila(tabla, "cor01paao011", "2017-04-24")
    # 225 publicado (acciones de hoy) = 0,45 por acción de entonces; coincide con el aviso de la SFC
    assert bvc["valor"] == "0,45" and bvc["factor_split"] == "0,002"
    assert bvc["estado"] == "verificado" and bvc["monto_sfc"] == "coincide" and bvc["regla_ex"] == "0"
    assert _fila(tabla, "cor01paao011", "2021-04-26")["fuentes"] == "investing+stockanalysis"


def test_duplicados_escisiones_y_exclusiones(datos):
    tabla, resumen = dividendos_web.detectar()
    sura = tabla.filter(pl.col("especie") == "cot13paao004")
    # El duplicado de julio queda en uno, con la fecha que confirma stockanalysis
    assert sura.filter(pl.col("fecha_ex").str.starts_with("2023-07")).get_column("fecha_ex").to_list() == ["2023-07-05"]
    assert resumen["duplicados"] == 1
    # La escisión valorada en 17.940 no está en stockanalysis y es la mitad del precio: se revisa
    esc = _fila(tabla, "cot13paao004", "2023-08-01")
    assert esc["estado"] == "revisar" and "stockanalysis" in esc["motivos"] and "precio" in esc["motivos"]
    # El monto anual del aviso (1.280 en cuatro cuotas) confirma las cuotas de 320
    assert _fila(tabla, "cot13paao004", "2023-04-04")["monto_sfc"] == "coincide"
    assert _fila(tabla, "cot13paao004", "2010-03-26")["motivos"] == "prueba de exclusión"


def test_revision_y_uso_en_retornos(datos):
    gold, resumen = dividendos_web.construir()
    assert resumen["aplicables"] == 5  # 2 BVC + 3 cuotas de Sura de 2023
    ruta = dividendos_web.ruta_revision()
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        cab, *filas = list(csv.reader(f, delimiter=";"))
    for fila in filas:
        if fila[cab.index("fecha_ex")] == "2023-09-26":
            fila[cab.index("revision")] = "rechazado"
        if fila[cab.index("fecha_ex")] == "2010-03-26":
            fila[cab.index("revision")], fila[cab.index("valor_revisado")] = "corregido", "670"
            fila[cab.index("fecha_ex")] = "26/03/2010"  # como la guarda Excel
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=";").writerows([cab, *filas])
    gold, resumen = dividendos_web.construir()
    fechas = gold.get_column("fecha_ex").to_list()
    assert d(2023, 9, 26) not in fechas and d(2010, 3, 26) in fechas
    assert gold.filter(pl.col("fecha_ex") == d(2010, 3, 26)).get_column("dividendo")[0] == 670
    assert dividendos_web.leer_revision().height == len(filas)  # sin duplicar filas

    storage.escribir_completa("gold", "fact_split", storage.leer_tabla("gold", "fact_split"))
    df, res = retornos.construir()
    dia = df.filter((pl.col("especie") == "cor01paao011") & (pl.col("fecha") == d(2017, 4, 24))).row(0, named=True)
    assert dia["dividendo"] == pytest.approx(0.45)
    assert dia["retorno_total"] == pytest.approx((22.0 + 0.45) / 22.5 - 1)
    assert res["dividendos_web"] >= 5


def test_pagos_en_dolares_mezclados(datos, monkeypatch):
    # Mineros: Investing mezcla pagos en dólares (marcados USD) con pagos ya convertidos a pesos
    cfg = {**CFG, "activos": [{"especie": "coc07paao002", "investing": "mineros-sa"}]}
    monkeypatch.setattr(dividendos_web, "config", lambda: cfg)
    _escribir(datos, "investing", "mineros-sa", [
        "2025-04-25;0.025;3M;2025-05-02;USD", "2025-07-28;102.68;3M;2025-08-01",
        "2025-10-28;96.03;3M;2025-11-04", "2026-01-27;0.025;3M;2026-02-02;USD"])
    storage.upsert_por_anio("silver", "fact_fx_diaria", pl.DataFrame([{
        "fecha": f, "moneda_origen": "USD", "moneda_destino": "COP", "tasa": t, "fuente_id": "trm_datos_gov",
        "ingest_ts": AHORA} for f, t in [(d(2025, 4, 25), 4200.0), (d(2026, 1, 27), 3900.0)]]), ("fecha",))
    precios = [{"fecha": f, "especie": "coc07paao002", "codigo_titulo": "COC07PAAO002", "precio_promedio": 4000.0,
                "valor_transado": 1e9, "fuente_id": "sfc_precios_promedio", "ingest_ts": AHORA}
               for f in (d(2025, 4, 24), d(2025, 4, 25), d(2025, 7, 25), d(2025, 7, 28), d(2025, 10, 27),
                         d(2025, 10, 28), d(2026, 1, 26), d(2026, 1, 27))]
    storage.upsert_por_anio("silver", "fact_precio_sfc", pl.DataFrame(precios), ("fecha", "especie"))
    tabla, _ = dividendos_web.detectar()
    usd = _fila(tabla, "coc07paao002", "2025-04-25")
    # 0,025 USD × 4.200 = 105 pesos sobre 4.000: 2,6 %, sin alerta de mediana frente a los pagos en pesos
    assert usd["moneda"] == "USD" and usd["rendimiento"] == "0,02625" and usd["estado"] == "verificado"
    assert _fila(tabla, "coc07paao002", "2025-07-28")["moneda"] == "COP"
    gold, _ = dividendos_web.construir()
    df, res = retornos.construir()
    dia = df.filter((pl.col("especie") == "coc07paao002") & (pl.col("fecha") == d(2026, 1, 27))).row(0, named=True)
    assert dia["dividendo"] == pytest.approx(0.025 * 3900)
