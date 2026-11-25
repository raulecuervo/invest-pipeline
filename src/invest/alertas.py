"""Alertas: hechos que piden la atención del usuario, calculados al momento sobre la base del modelo.

Automáticas (no hay que configurarlas):

- ``cartera_banda``: una clase de una cartera está fuera de la banda de su perfil.
- ``concentracion``: un emisor pesa más que el máximo por emisor del perfil.
- ``caida_fuerte``: una acción que tiene cayó 5 % o más en su último día de negocio (de los últimos 5).
- ``dividendo_ex``: una acción que tiene entra en fecha ex-dividendo en los próximos 7 días.
- ``tesis_objetivo``: el precio alcanzó el precio objetivo de una tesis.
- ``tesis_revision``: la fecha de revisión de una tesis ya pasó.
- ``valoracion_extremo``: una acción que tiene o con tesis está a 2 desviaciones o más de su P/VL medio de
  5 años.
- ``datos``: una fuente de datos está atrasada o falló (salvo las manuales).

Propias (``usuario/dim_alerta``): precio, P/VL, z del P/VL o rendimiento por dividendo de una acción frente
a un umbral.

Son avisos para mirar, no recomendaciones. Cada alerta tiene una ``clave`` estable (tipo, cartera, acción y
fecha de referencia, esta última solo en los hechos puntuales) para que ``invest alertas --nuevas`` muestre
solo las que no se habían visto (``~/.invest/alertas_vistas.json``, fuera del repositorio de datos).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import duckdb
import polars as pl

from invest import usuario

SEVERIDADES = ("atencion", "info")
CAIDA_FUERTE = -0.05
DIAS_DIVIDENDO = 7
Z_EXTREMO = 2.0
PUNTUALES = {"caida_fuerte", "dividendo_ex", "tesis_objetivo"}

CONDICIONES = {
    "precio_mayor_que": ("precio", ">", "el precio superó"),
    "precio_menor_que": ("precio", "<", "el precio bajó de"),
    "p_vl_menor_que": ("p_vl", "<", "el P/VL bajó de"),
    "p_vl_mayor_que": ("p_vl", ">", "el P/VL superó"),
    "z_p_vl_menor_que": ("z_p_vl", "<", "el z del P/VL (5 años) bajó de"),
    "z_p_vl_mayor_que": ("z_p_vl", ">", "el z del P/VL (5 años) superó"),
    "rendimiento_dividendo_mayor_que": ("rendimiento_dividendo_12m", ">", "el rendimiento por dividendo superó"),
}


def _num(x: float, d: int = 0) -> str:
    return f"{x:,.{d}f}".replace(",", "§").replace(".", ",").replace("§", ".")


MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _fecha(x) -> str:
    if not x:
        return "—"
    d = x if isinstance(x, date) else date.fromisoformat(str(x)[:10])
    return f"{d.day} {MESES[d.month - 1]} {d.year}"


def _pct(x: float, d: int = 1) -> str:
    return _num(x * 100, d) + " %"


def _filas(con, sql: str, params: list | None = None) -> list[dict]:
    try:
        cur = con.execute(sql, params or [])
    except duckdb.Error:
        return []  # falta una tabla (base nueva o sin carteras): la regla no aplica
    nombres = [c[0] for c in cur.description]
    # Un DECIMAL de DuckDB llega como Decimal: se pasa a float para operar y formatear
    return [{k: float(v) if isinstance(v, Decimal) else v for k, v in zip(nombres, r)} for r in cur.fetchall()]


def _alerta(tipo, severidad, titulo, detalle, fecha_ref, cartera_id=None, especie=None, enlace=None, extra="") -> dict:
    # La clave sirve para avisar una sola vez: lleva la fecha solo en los hechos puntuales (una caída, una
    # fecha ex); una condición que sigue vigente (fuera de banda, tesis vencida…) no se repite cada día
    fecha_clave = fecha_ref if tipo in PUNTUALES else ""
    clave = "|".join(str(x) for x in (tipo, cartera_id or "", especie or "", fecha_clave or "", extra))
    return {"clave": clave, "tipo": tipo, "severidad": severidad, "titulo": titulo, "detalle": detalle,
            "fecha": fecha_ref, "cartera_id": cartera_id, "especie": especie, "enlace": enlace}


_NOMBRE = """coalesce(a.nemotecnico, e.nombre_corto, {x}.especie)"""
_JOIN_NOMBRE = """LEFT JOIN dim_activo a ON a.especie = {x}.especie LEFT JOIN dim_emisor e ON e.emisor_id = a.emisor_id"""


def _posiciones_actuales(con) -> list[dict]:
    return _filas(con, f"""
        SELECT p.cartera_id, p.especie, p.valor_cop, p.peso, {_NOMBRE.format(x='p')} AS nombre,
               coalesce(e.nombre_corto, a.emisor_id) AS emisor
        FROM fact_posicion p {_JOIN_NOMBRE.format(x='p')}
        WHERE p.especie <> 'EFECTIVO'
          AND p.fecha = (SELECT max(fecha) FROM fact_posicion q WHERE q.cartera_id = p.cartera_id)""")


def calcular(con, hoy: date | None = None) -> list[dict]:
    hoy = hoy or date.today()
    salida: list[dict] = []
    posiciones = _posiciones_actuales(con)
    tenidas = sorted({p["especie"] for p in posiciones})
    carteras = {r["cartera_id"]: r["nombre"] for r in _filas(con, "SELECT cartera_id, nombre FROM dim_cartera")}
    clases = {r["clase_activo_id"]: r["nombre"] for r in _filas(con, "SELECT clase_activo_id, clase AS nombre FROM dim_clase_activo")}
    clases["EFECTIVO"] = "Efectivo"

    # Cartera: clases fuera de banda
    for r in _filas(con, """
            SELECT x.* FROM fact_asignacion x
            WHERE x.fuera_de_banda AND x.fecha = (SELECT max(fecha) FROM fact_asignacion y WHERE y.cartera_id = x.cartera_id)"""):
        clase = clases.get(r["clase_activo_id"], r["clase_activo_id"])
        salida.append(_alerta(
            "cartera_banda", "atencion", f"{carteras.get(r['cartera_id'], r['cartera_id'])}: {clase} fuera de banda",
            f"Pesa {_pct(r['peso'])} frente a un objetivo de {_pct(r['peso_objetivo'])} (banda {_pct(r['banda_minima'] or 0)} "
            f"a {_pct(r['banda_maxima'] or 1)}). La calculadora de rebalanceo de Mi cartera dice cuánto mover.",
            str(r["fecha"]), cartera_id=r["cartera_id"], enlace="#/cartera/" + r["cartera_id"]))

    # Cartera: concentración por emisor frente al máximo del perfil
    maximos = {r["cartera_id"]: r["m"] for r in _filas(con, """
        SELECT c.cartera_id, p.maximo_por_emisor AS m FROM dim_cartera c
        JOIN dim_perfil p ON p.perfil_id = c.perfil_id
        QUALIFY row_number() OVER (PARTITION BY c.cartera_id ORDER BY p.version DESC) = 1""") if r["m"]}
    por_emisor: dict[tuple, float] = {}
    for p in posiciones:
        clave = (p["cartera_id"], p["emisor"] or p["nombre"])
        por_emisor[clave] = por_emisor.get(clave, 0.0) + (p["peso"] or 0.0)
    for (cartera, emisor), peso in sorted(por_emisor.items()):
        m = maximos.get(cartera)
        if m and peso > m + 1e-9:
            salida.append(_alerta(
                "concentracion", "atencion", f"{carteras.get(cartera, cartera)}: {emisor} pesa {_pct(peso)}",
                f"Por encima del máximo por emisor de su perfil ({_pct(m, 0)}).", hoy.isoformat(),
                cartera_id=cartera, especie=emisor, enlace="#/cartera/" + cartera))

    if tenidas:
        marcas = ", ".join("?" for _ in tenidas)
        # Caída fuerte en el último día de negocio (si fue en los últimos 5 días)
        for r in _filas(con, f"""
                SELECT d.especie, d.fecha, d.retorno_total, d.precio, {_NOMBRE.format(x='d')} AS nombre
                FROM fact_precio_diario d {_JOIN_NOMBRE.format(x='d')}
                WHERE d.especie IN ({marcas}) AND d.fecha >= ?
                QUALIFY row_number() OVER (PARTITION BY d.especie ORDER BY d.fecha DESC) = 1""",
                [*tenidas, hoy - timedelta(days=5)]):
            if r["retorno_total"] is not None and r["retorno_total"] <= CAIDA_FUERTE:
                salida.append(_alerta(
                    "caida_fuerte", "atencion", f"{r['nombre']} cayó {_pct(-r['retorno_total'])} en el día",
                    f"Cerró en {_num(r['precio'])} el {_fecha(r['fecha'])}. Revise la información relevante de la ficha.",
                    str(r["fecha"]), especie=r["especie"], enlace="#/ficha/" + r["especie"]))
        # Fecha ex-dividendo próxima de una acción que tiene
        for r in _filas(con, f"""
                SELECT DISTINCT a.especie, c.fecha, c.valor, c.moneda, {_NOMBRE.format(x='a')} AS nombre
                FROM fact_evento_calendario c JOIN dim_activo a ON a.emisor_id = c.emisor_id
                     AND (c.especie IS NULL OR c.especie = a.especie)
                LEFT JOIN dim_emisor e ON e.emisor_id = a.emisor_id
                WHERE c.tipo = 'dividendo_ex' AND a.especie IN ({marcas}) AND c.fecha BETWEEN ? AND ?""",
                [*tenidas, hoy, hoy + timedelta(days=DIAS_DIVIDENDO)]):
            valor = f" de {_num(r['valor'], 2)} {r['moneda'] or 'COP'} por acción" if r["valor"] else ""
            salida.append(_alerta(
                "dividendo_ex", "info", f"{r['nombre']}: fecha ex-dividendo el {_fecha(r['fecha'])}",
                f"Quien compre desde ese día ya no recibe el dividendo{valor}; usted lo recibe si tenía la acción antes.",
                str(r["fecha"]), especie=r["especie"], enlace="#/ficha/" + r["especie"]))

    # Tesis: precio objetivo alcanzado y revisión vencida
    for r in _filas(con, f"""
            SELECT t.*, {_NOMBRE.format(x='t')} AS nombre, d.precio, d.fecha AS fecha_precio
            FROM dim_tesis t {_JOIN_NOMBRE.format(x='t')}
            LEFT JOIN (SELECT especie, precio, fecha FROM fact_precio_diario
                       QUALIFY row_number() OVER (PARTITION BY especie ORDER BY fecha DESC) = 1) d ON d.especie = t.especie"""):
        cartera = carteras.get(r["cartera_id"], r["cartera_id"])
        if r.get("precio_objetivo") and r.get("precio") and r["precio"] >= r["precio_objetivo"]:
            salida.append(_alerta(
                "tesis_objetivo", "info", f"{r['nombre']} alcanzó el precio objetivo de su tesis",
                f"Precio {_num(r['precio'])} (al {_fecha(r['fecha_precio'])}) frente a un objetivo de {_num(r['precio_objetivo'])} "
                f"en {cartera}. Momento de revisar si la tesis sigue en pie.",
                str(r["fecha_precio"]), cartera_id=r["cartera_id"], especie=r["especie"], enlace="#/tesis"))
        if r.get("fecha_revision") and r["fecha_revision"] <= hoy:
            salida.append(_alerta(
                "tesis_revision", "atencion", f"Revisión vencida: tesis de {r['nombre']}",
                f"La fecha de revisión era el {_fecha(r['fecha_revision'])} ({cartera}).",
                str(r["fecha_revision"]), cartera_id=r["cartera_id"], especie=r["especie"], enlace="#/tesis"))

    # Valoración en un extremo de su historia (acciones que tiene o con tesis)
    seguidas = sorted(set(tenidas) | {r["especie"] for r in _filas(con, "SELECT DISTINCT especie FROM dim_tesis")})
    if seguidas:
        marcas = ", ".join("?" for _ in seguidas)
        for r in _filas(con, f"""
                SELECT b.especie, b.fecha, b.z, b.actual, b.media, {_NOMBRE.format(x='b')} AS nombre
                FROM fact_banda_valoracion b {_JOIN_NOMBRE.format(x='b')}
                WHERE b.indicador = 'p_vl' AND b.ventana = '5a' AND b.suficiente AND NOT coalesce(b.quiebre, false)
                  AND b.especie IN ({marcas}) AND abs(b.z) >= ?
                QUALIFY row_number() OVER (PARTITION BY b.especie ORDER BY b.fecha DESC) = 1""", [*seguidas, Z_EXTREMO]):
            lado = "por debajo" if r["z"] < 0 else "por encima"
            salida.append(_alerta(
                "valoracion_extremo", "info", f"{r['nombre']}: P/VL a {_num(abs(r['z']), 1)} desviaciones {lado} de su media",
                f"P/VL {_num(r['actual'], 2)} frente a una media de 5 años de {_num(r['media'], 2)}. Un extremo no es una señal "
                "por sí solo: revise si cambió algo en la empresa.", str(r["fecha"]), especie=r["especie"],
                enlace="#/valoracion/" + r["especie"]))

    # Reglas propias
    reglas = usuario.leer("dim_alerta") if (usuario.carpeta() / "dim_alerta.parquet").exists() else pl.DataFrame()
    if reglas.height:
        reglas = reglas.filter(pl.col("activa") == "si")
    if reglas.height:
        especies = reglas.get_column("especie").unique().to_list()
        marcas = ", ".join("?" for _ in especies)
        ultimos = {r["especie"]: r for r in _filas(con, f"""
            SELECT v.especie, v.fecha, v.precio, v.p_vl, v.rendimiento_dividendo_12m, {_NOMBRE.format(x='v')} AS nombre
            FROM fact_valoracion v {_JOIN_NOMBRE.format(x='v')} WHERE v.especie IN ({marcas})
            QUALIFY row_number() OVER (PARTITION BY v.especie ORDER BY v.fecha DESC) = 1""", especies)}
        zs = {r["especie"]: r["z"] for r in _filas(con, f"""
            SELECT especie, z FROM fact_banda_valoracion WHERE indicador = 'p_vl' AND ventana = '5a' AND especie IN ({marcas})
            QUALIFY row_number() OVER (PARTITION BY especie ORDER BY fecha DESC) = 1""", especies)}
        for regla in reglas.iter_rows(named=True):
            u = ultimos.get(regla["especie"])
            campo, signo, texto = CONDICIONES.get(regla["condicion"], (None, None, None))
            if not u or campo is None:
                continue
            valor = zs.get(regla["especie"]) if campo == "z_p_vl" else u.get(campo)
            if valor is None:
                continue
            if (valor > regla["umbral"]) if signo == ">" else (valor < regla["umbral"]):
                fmt = (lambda x: _pct(x)) if campo == "rendimiento_dividendo_12m" else (
                    (lambda x: _num(x, 2)) if campo in ("p_vl", "z_p_vl") else (lambda x: _num(x)))
                salida.append(_alerta(
                    "propia", "atencion", f"{u['nombre']}: {texto} {fmt(regla['umbral'])}",
                    f"Valor al {_fecha(u['fecha'])}: {fmt(valor)}." + (f" Nota: {regla['nota']}" if regla.get("nota") else ""),
                    str(u["fecha"]), especie=regla["especie"], enlace="#/ficha/" + regla["especie"], extra=regla["alerta_id"]))

    # Datos atrasados o con error (salvo las fuentes manuales)
    for r in _filas(con, "SELECT fuente_id, estado, ultimo_dato FROM salud_datos"):
        if r["estado"] != "al día" and not str(r["estado"]).startswith("manual"):
            salida.append(_alerta(
                "datos", "info", f"Fuente {r['fuente_id']}: {r['estado']}",
                f"Último dato: {_fecha(r['ultimo_dato'])}. Detalle en Salud de datos.", str(r["ultimo_dato"] or ""),
                enlace="#/salud"))

    orden = {s: i for i, s in enumerate(SEVERIDADES)}
    return sorted(salida, key=lambda a: (orden[a["severidad"]], a["tipo"], a["titulo"]))


# ----------------------------------------------------------------------------- vistas (CLI y notificación)
def _archivo_vistas():
    # Fuera del repositorio de datos: es un estado de este PC (qué notificaciones ya se mostraron) y no debe
    # viajar por git ni chocar con otra máquina
    import os
    from pathlib import Path

    carpeta = Path(os.environ.get("INVEST_ESTADO_LOCAL") or Path.home() / ".invest")
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / "alertas_vistas.json"


def nuevas(alertas: list[dict], marcar: bool = True) -> list[dict]:
    """Las alertas cuya clave no se había visto; si ``marcar``, quedan como vistas."""
    archivo = _archivo_vistas()
    vistas = set(json.loads(archivo.read_text(encoding="utf-8"))) if archivo.exists() else set()
    salida = [a for a in alertas if a["clave"] not in vistas]
    if marcar:
        # Se guardan solo las vigentes: una alerta que desaparece y vuelve se avisa de nuevo
        archivo.write_text(json.dumps(sorted(a["clave"] for a in alertas), ensure_ascii=False, indent=1), encoding="utf-8")
    return salida
