import csv
from datetime import date, datetime, timezone

import polars as pl
import pytest

from invest import retornos, splits, storage

AHORA = datetime(2026, 11, 22, tzinfo=timezone.utc)
d = date


def _precios(filas):
    return pl.DataFrame([{"fecha": f, "especie": e, "codigo_titulo": e.upper(), "precio_promedio": p,
                          "valor_transado": 1e9, "fuente_id": "sfc_precios_promedio", "ingest_ts": AHORA}
                         for e, f, p in filas])


@pytest.fixture
def mercado(carpeta_datos):
    filas = [
        # Celsia 2010: split 1:10 con evento de la SFC
        ("cot60paao005", d(2010, 4, 21), 59000.0), ("cot60paao005", d(2010, 4, 22), 59500.0),
        ("cot60paao005", d(2010, 4, 23), 5901.8), ("cot60paao005", d(2010, 4, 26), 5950.0),
        # BVC 2018: reagrupación 500:1, sin evento cargado -> requiere aprobación
        ("cor01paao011", d(2018, 4, 27), 22.5), ("cor01paao011", d(2018, 5, 2), 11188.7),
        ("cor01paao011", d(2018, 5, 3), 11250.0),
        # Error puntual de la fuente: sube y vuelve
        ("cod30paao001", d(1998, 11, 9), 130.0), ("cod30paao001", d(1998, 11, 10), 1134.0),
        ("cod30paao001", d(1998, 11, 11), 131.0),
        # Salto que se sostiene pero no se parece a un factor redondo
        ("coa36paao005", d(2015, 6, 1), 1000.0), ("coa36paao005", d(2015, 6, 2), 1700.0),
        ("coa36paao005", d(2015, 6, 3), 1710.0),
    ]
    storage.upsert_por_anio("silver", "fact_precio_sfc", _precios(filas), ("fecha", "especie"))
    storage.escribir_completa("gold", "dim_activo", pl.DataFrame({
        "especie": ["cot60paao005", "cor01paao011", "cod30paao001", "coa36paao005"],
        "emisor_id": ["CO-COT60", "CO-COR01", "CO-COD30", "CO-COA36"],
        "nombre": ["AO CELSIA S.A.", "AO BOLSA DE VALORES DE COLOMBIA S.A.", "AO ETERNIT COLOMBIANA S.A.",
                   "AO AGROGUACHAL S.A."],
        "clase_accion": ["ordinaria"] * 4}))
    storage.escribir_por_anio("gold", "fact_evento_corporativo", pl.DataFrame([{
        "evento_id": "ev1", "fecha": d(2010, 3, 30), "hora": "17:00:00", "entidad": "CELSIA S.A.",
        "tema": "Decisiones de Asamblea", "resumen": "La Asamblea aprobó la reforma del valor nominal de la acción.",
        "tiene_anexo": True, "anexo_archivo": None, "fuente_id": "sfc_informacion_relevante", "ingest_ts": AHORA,
        "categoria": "asamblea_decision", "afecta_precio": False, "emisor_id": "CO-COT60"}]), "fecha")
    return carpeta_datos


def test_factor_redondo():
    assert splits.factor_redondo(5901.8 / 59500) == 10          # caída de 90 %
    assert splits.factor_redondo(11188.7 / 22.5) == pytest.approx(1 / 500)
    assert splits.factor_redondo(1700 / 1000) is None             # 70 % no es un factor redondo
    assert splits._numero("1:10") == 10 and splits._numero("500:1") == pytest.approx(0.002)
    assert splits._numero("0,002") == pytest.approx(0.002)


def test_clasifica_y_aplica_solo_lo_respaldado(mercado):
    tabla, resumen = splits.construir()
    # El error puntual cuenta dos días: el salto y el regreso
    assert resumen == {"split_con_evento": 1, "split_sin_evento": 1, "sin_explicacion": 1,
                       "error_puntual": 2, "aplicados": 1, "mercado": 0, "atipico": 0, "por_revisar": 2}
    rev = splits.leer_revision()
    assert set(rev.get_column("especie")) == {"cot60paao005", "cor01paao011", "coa36paao005"}  # sin el error
    celsia = rev.filter(pl.col("especie") == "cot60paao005").row(0, named=True)
    assert celsia["eventos"] == "ev1" and "valor nominal" in celsia["resumen_evento"]
    assert tabla.rows() == [("cot60paao005", d(2010, 4, 23), 10.0, "automatico", "split_con_evento")]

    df, res = retornos.construir()
    dia = df.filter((pl.col("especie") == "cot60paao005") & (pl.col("fecha") == d(2010, 4, 23))).row(0, named=True)
    assert dia["split_factor"] == 10 and dia["retorno_precio"] == pytest.approx(5901.8 / 5950 - 1)
    assert dia["salto_atipico"] is False and res["splits_aplicados"] == 1
    # La BVC sigue marcada hasta que se apruebe
    bvc = df.filter((pl.col("especie") == "cor01paao011") & (pl.col("fecha") == d(2018, 5, 2))).row(0, named=True)
    assert bvc["salto_atipico"] is True


def _revisar(cambios):
    ruta = splits.ruta_revision()
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        cab, *filas = list(csv.reader(f, delimiter=";"))
    for fila in filas:
        for especie, campos in cambios.items():
            if fila[cab.index("especie")] == especie:
                for k, v in campos.items():
                    fila[cab.index(k)] = v
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=";").writerows([cab, *filas])


def test_revision_humana_manda(mercado):
    splits.construir()
    # Aprueba la reagrupación de la BVC, rechaza el split de Celsia y corrige el salto sin explicación
    _revisar({"cor01paao011": {"revision": "aprobado"},
              "cot60paao005": {"revision": "rechazado"},
              "coa36paao005": {"revision": "corregido", "factor_revisado": "1:2", "notas_revision": "prueba"}})
    tabla, resumen = splits.construir()
    assert sorted(tabla.select("especie", "factor_acciones").rows()) == [
        ("coa36paao005", 2.0), ("cor01paao011", pytest.approx(0.002))]
    # Lo decidido no se pierde al volver a correr
    rev = splits.leer_revision()
    assert rev.filter(pl.col("especie") == "coa36paao005").row(0, named=True)["notas_revision"] == "prueba"

    df, _ = retornos.construir()
    bvc = df.filter((pl.col("especie") == "cor01paao011") & (pl.col("fecha") == d(2018, 5, 2))).row(0, named=True)
    assert bvc["retorno_precio"] == pytest.approx(11188.7 / (22.5 * 500) - 1) and bvc["salto_atipico"] is False


def test_csv_guardado_por_excel(mercado):
    """Excel cambia 2018-05-02 por 02/05/2018 y "1:10" por la hora "1:10:00"."""
    splits.construir()
    ruta = splits.ruta_revision()
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        cab, *filas = list(csv.reader(f, delimiter=";"))
    for fila in filas:
        a, m, dd = fila[cab.index("fecha")].split("-")
        fila[cab.index("fecha")] = f"{dd}/{m}/{a}"
        if fila[cab.index("especie")] == "coa36paao005":
            fila[cab.index("revision")], fila[cab.index("factor_revisado")] = "corregido", "1:2:00"
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=";").writerows([cab, *filas])
    tabla, _ = splits.construir()
    assert ("coa36paao005", d(2015, 6, 2), 2.0) in tabla.select("especie", "fecha", "factor_acciones").rows()
    assert splits.leer_revision().height == 3  # sin filas duplicadas por el cambio de formato


def test_salto_de_mercado_no_se_ajusta_ni_se_marca(mercado):
    """Una OPA o una quiebra mueven el precio de verdad: no es split, pero tampoco es un error."""
    splits.construir()
    _revisar({"coa36paao005": {"revision": "mercado", "notas_revision": "OPA"}})
    tabla, resumen = splits.construir()
    assert resumen["mercado"] == 1
    assert "coa36paao005" not in tabla.get_column("especie").to_list()
    assert storage.leer_tabla("gold", "fact_salto_mercado").rows() == [("coa36paao005", d(2015, 6, 2), "OPA")]

    df, res = retornos.construir()
    dia = df.filter((pl.col("especie") == "coa36paao005") & (pl.col("fecha") == d(2015, 6, 2))).row(0, named=True)
    assert dia["split_factor"] is None and dia["retorno_precio"] == pytest.approx(0.7)
    assert dia["salto_atipico"] is False and res["saltos_mercado"] == 1
