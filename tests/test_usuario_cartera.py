from datetime import date

import polars as pl

from invest import cartera, storage, usuario
from invest.calendario import sesiones_entre


def _transacciones():
    filas = [
        {"transaccion_id": "t1", "cartera_id": "c", "fecha_operacion": date(2025, 1, 2), "tipo": "aporte",
         "monto": 1_000_000.0, "moneda": "COP"},
        {"transaccion_id": "t2", "cartera_id": "c", "fecha_operacion": date(2025, 1, 3), "tipo": "compra",
         "especie": "coa01paao001", "cantidad": 100.0, "precio": 5000.0, "comision": 0.0, "moneda": "COP"},
        {"transaccion_id": "t3", "cartera_id": "c", "fecha_operacion": date(2025, 2, 14), "tipo": "aporte",
         "monto": 500_000.0, "moneda": "COP"},
        {"transaccion_id": "t4", "cartera_id": "c", "fecha_operacion": date(2025, 2, 20), "tipo": "venta",
         "especie": "coa01paao001", "cantidad": 40.0, "precio": 6000.0, "comision": 1000.0, "moneda": "COP"},
    ]
    return pl.DataFrame(filas, schema=usuario.TABLAS["fact_transaccion"]["columnas"])


def test_validacion_no_escribe_si_hay_problemas(carpeta_datos):
    malo = _transacciones().with_columns(pl.lit("trueque").alias("tipo"))
    problemas = usuario.guardar("fact_transaccion", malo)
    assert any("no permitidos" in p for p in problemas)
    assert usuario.leer("fact_transaccion").is_empty()
    assert usuario.guardar("fact_transaccion", _transacciones()) == []
    assert usuario.leer("fact_transaccion").height == 4
    escritos = usuario.plantillas()
    assert any(p.name == "dim_perfil.csv" for p in escritos)


def test_posiciones_costo_y_desempeno(carpeta_datos):
    sesiones = sesiones_entre(date(2025, 1, 2), date(2025, 2, 28))
    precios = [{"fecha": d, "especie": "coa01paao001", "precio": 5000.0 if d < date(2025, 1, 15) else 6000.0}
               for d in sesiones]
    storage.escribir_por_anio("gold", "fact_precio_diario", pl.DataFrame(precios), "fecha")
    usuario.guardar("dim_cartera", pl.DataFrame([{"cartera_id": "c", "nombre": "C", "tipo": "real", "moneda_base": "COP",
                                                   "perfil_id": "p"}]))
    usuario.guardar("dim_perfil", pl.DataFrame([{"perfil_id": "p", "version": 1, "nombre": "P", "objetivo": "crecimiento",
                                                  "moneda_base": "COP", "vigente_desde": date(2025, 1, 1)}]))
    usuario.guardar("dim_perfil_asignacion", pl.DataFrame([
        {"perfil_id": "p", "version": 1, "clase_activo_id": "ACCION", "peso_objetivo": 0.6, "banda_minima": 0.5,
         "banda_maxima": 0.7}]))
    t = _transacciones()
    pos = cartera.fact_posicion(t, hasta=date(2025, 2, 28))
    fin_ene = pos.filter(pl.col("fecha") == date(2025, 1, 31))
    accion = fin_ene.filter(pl.col("especie") == "coa01paao001").row(0, named=True)
    caja = fin_ene.filter(pl.col("especie") == cartera.EFECTIVO).row(0, named=True)
    assert accion["cantidad"] == 100 and accion["costo_fiscal_cop"] == 500_000 and accion["valor_cop"] == 600_000
    assert caja["valor_cop"] == 500_000
    fin_feb = pos.filter(pl.col("fecha") == date(2025, 2, 28))
    accion = fin_feb.filter(pl.col("especie") == "coa01paao001").row(0, named=True)
    # Vendió 40 de 100: el costo fiscal baja en la misma proporción
    assert accion["cantidad"] == 60 and accion["costo_fiscal_cop"] == 300_000
    caja = fin_feb.filter(pl.col("especie") == cartera.EFECTIVO).row(0, named=True)
    assert caja["valor_cop"] == 500_000 + 500_000 + 40 * 6000 - 1000
    asig = cartera.fact_asignacion(pos).filter((pl.col("fecha") == date(2025, 1, 31)) & (pl.col("clase_activo_id") == "ACCION"))
    # Sin dim_activo, la acción cae en OTRO; el objetivo de ACCION queda con peso 0 y fuera de banda
    assert asig.row(0, named=True)["fuera_de_banda"]
    d = cartera.fact_desempeno(pos, t).sort("mes")
    enero, febrero = d.row(0, named=True), d.row(1, named=True)
    assert abs(enero["retorno_mes"] - 0.10) < 1e-12  # 1.000.000 aportados valen 1.100.000
    assert abs(febrero["retorno_mes"] - (-1000 / (1_100_000 + 500_000 * 14 / 28))) < 1e-9  # solo la comisión
    assert enero["xirr_desde_inicio"] > 0.5  # 10 % en un mes, anualizado


def test_xirr_conocido():
    flujos = [(date(2024, 1, 1), -100.0), (date(2025, 1, 1), 110.0)]
    assert abs(cartera.xirr(flujos) - 0.10) < 1e-3


def test_activos_propios_con_precio_manual_y_fic_de_la_sfc(carpeta_datos):
    """BACCO (Mercado Global) se valora con el precio del extracto; el FIC con el valor de la unidad de la SFC
    cuando se empareja por nombre, y si no, con el del extracto."""
    sesiones = sesiones_entre(date(2026, 9, 1), date(2026, 10, 30))
    storage.escribir_por_anio("gold", "fact_precio_diario",
                              pl.DataFrame([{"fecha": d, "especie": "x", "precio": 1.0} for d in sesiones]), "fecha")
    usuario.guardar("dim_activo_usuario", pl.DataFrame([
        {"especie": "BACCO", "nombre": "Bank of America (MGC)", "clase_activo_id": "MGC", "pais_id": "US", "moneda_id": "COP"},
        {"especie": "FIC_VISTA", "nombre": "FIC Accival Vista", "clase_activo_id": "FIC", "fic_entidad": "Acciones",
         "fic_fondo": "accival vista"},
        {"especie": "FIC_180", "nombre": "Accival Renta Fija 180", "clase_activo_id": "FIC", "fic_entidad": "Acciones",
         "fic_fondo": "renta fija 180"}], schema=usuario.TABLAS["dim_activo_usuario"]["columnas"]))
    usuario.guardar("fact_precio_manual", pl.DataFrame([
        {"especie": "BACCO", "fecha": date(2026, 9, 30), "precio": 150000.0, "moneda": "COP"},
        {"especie": "FIC_180", "fecha": date(2026, 10, 30), "precio": 18400.0, "moneda": "COP"}],
        schema=usuario.TABLAS["fact_precio_manual"]["columnas"]))
    fic = pl.DataFrame({"fecha": [date(2026, 9, 30), date(2026, 9, 30), date(2026, 9, 30)],
                        "codigo_entidad": ["85", "85", "85"], "nombre_entidad": ["Acciones & Valores S.A."] * 3,
                        "codigo_negocio": ["1", "2", "2"], "nombre_fondo": ["FIC ACCIVAL VISTA", "ACCIVAL RENTA FIJA 180", "ACCIVAL RENTA FIJA 180"],
                        "tipo_participacion": ["501", "501", "502"], "valor_unidad": [10100.0, 18300.0, 18350.0]})
    storage.escribir_por_anio("silver", "fact_fic_mensual", fic, "fecha")
    t = pl.DataFrame([
        {"transaccion_id": "a", "cartera_id": "c", "fecha_operacion": date(2026, 9, 1), "tipo": "aporte", "monto": 200000.0, "moneda": "COP"},
        {"transaccion_id": "b", "cartera_id": "c", "fecha_operacion": date(2026, 9, 1), "tipo": "compra", "especie": "BACCO",
         "cantidad": 1.0, "precio": 136500.0, "moneda": "COP"},
        {"transaccion_id": "v", "cartera_id": "c", "fecha_operacion": date(2026, 9, 1), "tipo": "compra", "especie": "FIC_VISTA",
         "cantidad": 2.0, "precio": 10000.0, "moneda": "COP"},
        {"transaccion_id": "f", "cartera_id": "c", "fecha_operacion": date(2026, 9, 1), "tipo": "compra", "especie": "FIC_180",
         "cantidad": 1.0, "precio": 18000.0, "moneda": "COP"}], schema=usuario.TABLAS["fact_transaccion"]["columnas"])
    pos = cartera.fact_posicion(t, hasta=date(2026, 10, 30)).filter(pl.col("fecha") == date(2026, 10, 30))
    fila = {r["especie"]: r for r in pos.iter_rows(named=True)}
    assert (fila["BACCO"]["precio"], fila["BACCO"]["fuente_precio"], fila["BACCO"]["nombre"]) == (150000.0, "manual", "Bank of America (MGC)")
    assert (fila["FIC_VISTA"]["precio"], fila["FIC_VISTA"]["fuente_precio"]) == (10100.0, "fic_sfc")
    # Renta Fija 180 tiene dos participaciones: no se adivina, se usa el extracto
    assert (fila["FIC_180"]["precio"], fila["FIC_180"]["fuente_precio"]) == (18400.0, "manual")
    mapa, avisos = cartera.fic_emparejados(usuario.leer("dim_activo_usuario"), fic)
    assert mapa == {"FIC_VISTA": "fic:85:1:501"} and "2 fondos o participaciones" in avisos[0]
    clases = set(cartera.fact_asignacion(pos).get_column("clase_activo_id").to_list())
    assert {"MGC", "FIC", "EFECTIVO"} <= clases
