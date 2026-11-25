from datetime import date

import duckdb
import polars as pl

from invest import alertas, usuario

HOY = date(2026, 11, 25)


def _base():
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE dim_activo AS SELECT * FROM (VALUES ('coc04paao008', 'ECOPETROL', 'CO-COC04'), ('cod38paao005', 'CEMARGOS', 'CO-COD38'))
            t(especie, nemotecnico, emisor_id);
        CREATE TABLE dim_emisor AS SELECT * FROM (VALUES ('CO-COC04', 'Ecopetrol'), ('CO-COD38', 'Cementos Argos')) t(emisor_id, nombre_corto);
        CREATE TABLE dim_cartera AS SELECT 'c' AS cartera_id, 'Principal' AS nombre, 'p' AS perfil_id;
        CREATE TABLE dim_perfil AS SELECT 'p' AS perfil_id, 1 AS version, 0.15 AS maximo_por_emisor;
        CREATE TABLE dim_clase_activo AS SELECT 'ACCION' AS clase_activo_id, 'Acción' AS clase;
        CREATE TABLE fact_posicion AS SELECT * FROM (VALUES
            (DATE '2026-11-24', 'c', 'EFECTIVO', 10.0, 0.1), (DATE '2026-11-24', 'c', 'coc04paao008', 60.0, 0.6),
            (DATE '2026-11-24', 'c', 'cod38paao005', 30.0, 0.3)) t(fecha, cartera_id, especie, valor_cop, peso);
        CREATE TABLE fact_asignacion AS SELECT DATE '2026-11-24' AS fecha, 'c' AS cartera_id, 'ACCION' AS clase_activo_id,
            0.9 AS peso, 0.6 AS peso_objetivo, 0.5 AS banda_minima, 0.7 AS banda_maxima, true AS fuera_de_banda;
        CREATE TABLE fact_precio_diario AS SELECT * FROM (VALUES
            (DATE '2026-11-24', 'coc04paao008', 2000.0, -0.07), (DATE '2026-11-24', 'cod38paao005', 9600.0, 0.01))
            t(fecha, especie, precio, retorno_total);
        CREATE TABLE fact_evento_calendario AS SELECT DATE '2026-11-27' AS fecha, 'dividendo_ex' AS tipo, 'CO-COD38' AS emisor_id,
            'cod38paao005' AS especie, 107.5 AS valor, 'COP' AS moneda;
        CREATE TABLE dim_tesis AS SELECT 'c' AS cartera_id, 'cod38paao005' AS especie, 9000.0 AS precio_objetivo,
            DATE '2026-06-01' AS fecha_revision;
        CREATE TABLE fact_banda_valoracion AS SELECT DATE '2026-11-24' AS fecha, 'coc04paao008' AS especie, 'p_vl' AS indicador,
            '5a' AS ventana, -2.4 AS z, 0.8 AS actual, 1.4 AS media, true AS suficiente, false AS quiebre;
        CREATE TABLE fact_valoracion AS SELECT DATE '2026-11-24' AS fecha, 'coc04paao008' AS especie, 2000.0 AS precio,
            0.8 AS p_vl, 0.12 AS rendimiento_dividendo_12m;
        CREATE TABLE salud_datos AS SELECT * FROM (VALUES ('fred', 'error en la última ejecución', DATE '2026-11-23'),
            ('bvc_web', 'manual: sin lectura reciente', NULL)) t(fuente_id, estado, ultimo_dato);
    """)
    return con


def test_alertas_automaticas(carpeta_datos):
    a = alertas.calcular(_base(), hoy=HOY)
    tipos = sorted(x["tipo"] for x in a)
    assert tipos == sorted(["cartera_banda", "concentracion", "concentracion", "caida_fuerte", "dividendo_ex",
                            "tesis_objetivo", "tesis_revision", "valoracion_extremo", "datos"])
    # Lo que pide atención va primero; las fuentes manuales no alertan
    assert a[0]["severidad"] == "atencion" and a[-1]["severidad"] == "info"
    caida = next(x for x in a if x["tipo"] == "caida_fuerte")
    assert caida["titulo"] == "ECOPETROL cayó 7,0 % en el día" and caida["enlace"] == "#/ficha/coc04paao008"
    assert next(x for x in a if x["tipo"] == "dividendo_ex")["titulo"] == "CEMARGOS: fecha ex-dividendo el 27 nov 2026"


def test_alertas_propias_y_nuevas(carpeta_datos, tmp_path, monkeypatch):
    monkeypatch.setenv("INVEST_ESTADO_LOCAL", str(tmp_path / "estado"))
    filas = [{"alerta_id": "a1", "especie": "coc04paao008", "condicion": "precio_menor_que", "umbral": 2100.0, "activa": "si",
              "nota": "barata", "creada": HOY},
             {"alerta_id": "a2", "especie": "coc04paao008", "condicion": "rendimiento_dividendo_mayor_que", "umbral": 0.2,
              "activa": "si", "nota": None, "creada": HOY},
             {"alerta_id": "a3", "especie": "coc04paao008", "condicion": "z_p_vl_menor_que", "umbral": -2.0, "activa": "no",
              "nota": None, "creada": HOY}]
    assert usuario.guardar("dim_alerta", pl.DataFrame(filas, schema=usuario.TABLAS["dim_alerta"]["columnas"])) == []
    propias = [x for x in alertas.calcular(_base(), hoy=HOY) if x["tipo"] == "propia"]
    # a2 no se cumple (12 % < 20 %) y a3 está inactiva
    assert [x["titulo"] for x in propias] == ["ECOPETROL: el precio bajó de 2.100"]
    # --nuevas: la primera vez salen todas, la segunda ninguna; una condición vigente no se repite al día siguiente
    lista = alertas.calcular(_base(), hoy=HOY)
    assert len(alertas.nuevas(lista)) == len(lista) and alertas.nuevas(lista) == []
    otro_dia = alertas.calcular(_base(), hoy=date(2026, 11, 26))
    assert all(x["tipo"] in alertas.PUNTUALES for x in alertas.nuevas(otro_dia))


def test_base_sin_tablas_no_falla():
    assert alertas.calcular(duckdb.connect(), hoy=HOY) == []
