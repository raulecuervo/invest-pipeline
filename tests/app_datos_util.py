"""Conjunto de datos mínimo, sintético y sin internet, con el que todas las páginas del aplicativo
tienen algo que mostrar: seis acciones con tres años de precios, TRM e IPC, fundamentales, factores,
el modelo completo (``invest modelo``) y dos carteras con transacciones, perfil y tesis.

Escribe en la carpeta de ``INVEST_DATA_DIR`` (en las pruebas, el fixture ``carpeta_datos``).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from invest import storage, usuario
from invest.calendario import sesiones_entre
from invest.config import data_dir

ESPECIES = [f"coc{k:02d}paao00{k}" for k in range(6)]
NEMOS = ["ALFA", "BETA", "GAMA", "DELTA", "EPSI", "ZETA"]
SECTORES = ["BANCOS", "BANCOS", "ENERGIA", "ENERGIA", "INDUSTRIA", "INDUSTRIA"]


def _emisor(e: str) -> str:
    return f"CO-{e[:5].upper()}"


def dims() -> None:
    carpeta = data_dir() / "gold" / "dims"
    carpeta.mkdir(parents=True, exist_ok=True)
    n = len(ESPECIES)
    pl.DataFrame({
        "activo_id": [e.upper() for e in ESPECIES], "especie": ESPECIES, "nemotecnico": NEMOS,
        "nombre": [f"AO EMPRESA {x} S.A." for x in NEMOS], "emisor_id": [_emisor(e) for e in ESPECIES],
        "clase_accion": ["ordinaria"] * n, "sector_sfc": SECTORES, "activa": [True] * n,
        "dias_con_negociacion": [500] * n, "clase_activo_id": ["ACCION"] * n, "pais_id": ["CO"] * n,
        "moneda_id": ["COP"] * n, "iba_categoria": ["ALTA"] * n,
    }).write_parquet(carpeta / "dim_activo.parquet")


def mercado_y_macro(sesiones: list[date]) -> None:
    storage.upsert_por_anio("silver", "fact_fx_diaria", pl.DataFrame([
        {"fecha": d + timedelta(days=1), "moneda_origen": "USD", "moneda_destino": "COP", "tasa": 4000.0 + i,
         "fecha_mercado": d, "fuente_id": "trm"} for i, d in enumerate(sesiones)]), ["fecha"])
    meses = sorted({date(d.year, d.month, 1) for d in sesiones})
    storage.upsert_por_anio("silver", "fact_serie_macro/fuente=dane_ipc", pl.DataFrame([
        {"fecha": f, "serie_id": "ipc_co", "valor": 100 * 1.004 ** i, "unidad": "indice", "frecuencia": "mensual",
         "fecha_publicacion": f + timedelta(days=40), "publicacion_estimada": False, "fuente_id": "dane_ipc"}
        for i, f in enumerate(meses)]), ["fecha", "serie_id"])
    rnd = np.random.default_rng(7)
    comun = rnd.normal(0.0003, 0.01, len(sesiones))
    filas, liq = [], []
    for k, e in enumerate(ESPECIES):
        x = 1000.0 * (k + 1)
        ruido = rnd.normal(0.0002 * (k - 2), 0.008, len(sesiones))
        for i, d in enumerate(sesiones):
            r = float((0.6 + 0.15 * k) * comun[i] + ruido[i])
            x *= 1 + r
            filas.append({"fecha": d, "especie": e, "precio_promedio": x, "valor_transado": 1e9 * (k + 1),
                          "dias_desde_anterior": 1, "dividendo": None, "distribucion": None, "split_factor": None,
                          "retorno_precio": r, "retorno_total": r, "indice_rt": 100.0, "salto_atipico": False})
            liq.append({"fecha": d, "especie": e, "liquida": k < 5, "monto_medio_60": 1e9 * (k + 1)})  # la última acción no es líquida
    storage.escribir_por_anio("gold", "fact_retorno_diario", pl.DataFrame(filas), "fecha")
    storage.escribir_por_anio("gold", "fact_liquidez_diaria", pl.DataFrame(liq), "fecha")


def fundamentales(sesiones: list[date]) -> None:
    filas = []
    cortes = [c for c in (date(a, m, d) for a in range(sesiones[0].year - 1, sesiones[-1].year + 1)
                          for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))) if c < sesiones[-1] - timedelta(days=60)]
    for k, e in enumerate(ESPECIES):
        for j, c in enumerate(cortes):
            disp = c + timedelta(days=60)
            acumulado = (c.month // 3) * (20.0 + 5 * k) * (1 + 0.02 * j)
            for concepto, valor, unidad in (
                    ("acciones_circulacion", 1e8 * (k + 1), "acciones"),
                    ("utilidad_accion", acumulado, "pesos_por_accion"),
                    ("valor_patrimonial_accion", 800.0 * (k + 1) * (1 + 0.01 * j), "pesos_por_accion"),
                    ("dividendo_accion_ordinaria", 30.0 + 4 * k, "pesos_por_accion")):
                filas.append({"emisor_id": _emisor(e), "fecha_corte": c, "fecha_disponible": disp, "concepto": concepto,
                              "valor": valor, "unidad": unidad, "base": "separado", "fuente": "anexo_141"})
    storage.escribir_completa("gold", "fact_fundamental", pl.DataFrame(filas))


def datos_usuario(ultima: date) -> None:
    usuario.plantillas()
    inicio = ultima - timedelta(days=200)
    problemas = []
    problemas += usuario.guardar("dim_perfil", pl.DataFrame([{
        "perfil_id": "largo_plazo", "version": 1, "nombre": "Largo plazo", "objetivo": "crecimiento",
        "horizonte_anios": 10.0, "moneda_base": "COP", "perdida_maxima_aceptable": 0.3, "meta_retorno_real": 0.05,
        "benchmark": "universo_igual", "vigente_desde": inicio - timedelta(days=30)}], schema=usuario.TABLAS["dim_perfil"]["columnas"]))
    problemas += usuario.guardar("dim_perfil_asignacion", pl.DataFrame([
        {"perfil_id": "largo_plazo", "version": 1, "clase_activo_id": c, "pais_id": "CO", "moneda_id": "COP",
         "peso_objetivo": p, "banda_minima": p - 0.1, "banda_maxima": p + 0.1} for c, p in (("ACCION", 0.8), ("EFECTIVO", 0.2))],
        schema=usuario.TABLAS["dim_perfil_asignacion"]["columnas"]))
    problemas += usuario.guardar("dim_cartera", pl.DataFrame([
        {"cartera_id": "principal", "nombre": "Principal", "tipo": "real", "perfil_id": "largo_plazo", "moneda_base": "COP",
         "creada": inicio},
        {"cartera_id": "ensayo", "nombre": "Ensayo", "tipo": "hipotetica", "moneda_base": "COP", "creada": inicio}],
        schema=usuario.TABLAS["dim_cartera"]["columnas"]))
    trans = []
    for cid, especies in (("principal", ESPECIES[:3]), ("ensayo", ESPECIES[3:5])):
        trans.append({"transaccion_id": f"{cid}-0", "cartera_id": cid, "fecha_operacion": inicio, "tipo": "aporte",
                      "monto": 50_000_000.0, "moneda": "COP"})
        for j, e in enumerate(especies):
            trans.append({"transaccion_id": f"{cid}-{j + 1}", "cartera_id": cid, "fecha_operacion": inicio + timedelta(days=3),
                          "tipo": "compra", "especie": e, "cantidad": 1000.0, "precio": 1000.0 * (ESPECIES.index(e) + 1),
                          "moneda": "COP"})
    problemas += usuario.guardar("fact_transaccion", pl.DataFrame(trans, schema=usuario.TABLAS["fact_transaccion"]["columnas"],
                                                                  strict=False))
    problemas += usuario.guardar("dim_tesis", pl.DataFrame([{
        "cartera_id": "principal", "especie": ESPECIES[0], "tesis": "Crece el crédito", "catalizador": "Tasas a la baja",
        "precio_objetivo": 1500.0, "fecha_revision": ultima + timedelta(days=90), "creada": inicio, "actualizada": inicio}],
        schema=usuario.TABLAS["dim_tesis"]["columnas"], strict=False))
    assert not problemas, problemas


def construir(desde: date = date(2023, 1, 1), hasta: date | None = None) -> dict:
    """Arma la carpeta completa y corre el modelo. Devuelve el resumen de ``modelo.construir``."""
    from invest import factores, ficha, macro, mercado, modelo

    hasta = hasta or date.today() - timedelta(days=1)
    sesiones = sesiones_entre(desde, hasta)
    dims()
    mercado_y_macro(sesiones)
    fundamentales(sesiones)
    mercado.construir()
    macro.construir()
    tabla, _ = factores.construir()
    if tabla is not None:
        factores.validar(tabla)
        ficha.construir()
    datos_usuario(sesiones[-1])
    return modelo.construir()
