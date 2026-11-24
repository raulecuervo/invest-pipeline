"""Aplicativo local: servidor web pequeño sobre ``modelo.duckdb`` (``invest app``).

Solo escucha en 127.0.0.1: no se expone a la red. Lee del modelo (una conexión de solo lectura por
consulta) y escribe únicamente en ``invest-data/usuario/`` con ``invest.usuario.guardar``, que valida
antes de tocar el archivo; después de cada cambio recalcula posiciones y desempeño de las carteras.
Las acciones largas (leer la Bolsa de Colombia, actualizar el modelo) corren como procesos aparte y
la página consulta su estado.

Las páginas están en ``static/`` (HTML, CSS y JavaScript sin librerías externas).

Protección frente a otras páginas abiertas en el mismo navegador (``proteger_local``):

- Solo se atienden pedidos dirigidos a ``127.0.0.1`` o ``localhost`` (encabezado Host). Así una
  página externa no puede leer los datos con un cambio de DNS que apunte su dominio a este equipo.
- Todo lo que no sea lectura (POST) exige el encabezado ``X-Invest`` y, si el navegador indica el
  origen, que sea este mismo servidor. Un formulario o un ``fetch`` desde otro sitio no puede poner
  ese encabezado sin una consulta previa (CORS) que este servidor no autoriza.
- El navegador marca con ``Sec-Fetch-Site`` si el pedido viene de otro sitio; esos se rechazan.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from invest import modelo, usuario
from invest.config import data_dir

ESTATICOS = Path(__file__).parent / "static"
app = FastAPI(title="Invest", docs_url=None, redoc_url=None, openapi_url=None)
_bloqueo = threading.Lock()

HOSTS_LOCALES = {"127.0.0.1", "localhost", "[::1]"}
ENCABEZADO_PROPIO = "x-invest"


def _host(valor: str | None) -> str:
    """Nombre del equipo sin puerto: '127.0.0.1:8765' → '127.0.0.1'; '[::1]:80' → '[::1]'."""
    if not valor:
        return ""
    valor = valor.strip().lower()
    if valor.startswith("["):
        return valor.split("]")[0] + "]"
    return valor.split(":")[0]


def motivo_rechazo(metodo: str, encabezados) -> str | None:
    """Por qué no atender un pedido, o None si es de este mismo equipo y aplicativo."""
    if _host(encabezados.get("host")) not in HOSTS_LOCALES:
        return "host no permitido"
    sitio = encabezados.get("sec-fetch-site")
    if sitio and sitio not in ("same-origin", "none"):
        return "pedido desde otro sitio"
    origen = encabezados.get("origin")
    if origen and _host(urlsplit(origen).netloc) not in HOSTS_LOCALES:
        return "origen no permitido"
    if metodo not in ("GET", "HEAD", "OPTIONS") and encabezados.get(ENCABEZADO_PROPIO) != "1":
        return "falta el encabezado X-Invest"
    return None


@app.middleware("http")
async def proteger_local(request: Request, siguiente):
    motivo = motivo_rechazo(request.method, request.headers)
    if motivo:
        return JSONResponse({"detail": f"Rechazado: {motivo}"}, status_code=403)
    return await siguiente(request)


# --------------------------------------------------------------------------- utilidades
def _limpio(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _limpio(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_limpio(x) for x in v]
    return v


def responder(datos: Any) -> Response:
    return Response(json.dumps(_limpio(datos), ensure_ascii=False, allow_nan=False), media_type="application/json")


def ruta_base() -> Path:
    return data_dir() / "modelo.duckdb"


def asegurar_base() -> None:
    if not ruta_base().exists():
        with _bloqueo:
            if not ruta_base().exists():
                modelo.construir_duckdb()


def q(sql: str, params: list | None = None) -> list[dict]:
    asegurar_base()
    with duckdb.connect(str(ruta_base()), read_only=True) as con:
        try:
            cur = con.execute(sql, params or [])
            columnas = [c[0] for c in cur.description]
            return [dict(zip(columnas, fila)) for fila in cur.fetchall()]
        except duckdb.CatalogException:
            return []  # la tabla todavía no existe (p. ej. antes de la primera cartera)


def q1(sql: str, params: list | None = None) -> dict | None:
    filas = q(sql, params)
    return filas[0] if filas else None


def _tras_cambio_usuario() -> None:
    """Recalcula carteras y rehace la base para que las páginas vean el cambio."""
    from invest import cartera

    with _bloqueo:
        cartera.construir()
        modelo.construir_duckdb()


# --------------------------------------------------------------------------- estado y búsqueda
def huella_codigo() -> str:
    """Fecha y tamaño de los .py del paquete: cambia si se actualizó el código (``git pull``)."""
    base = Path(__file__).resolve().parents[1]
    partes = sorted((str(f.relative_to(base)), f.stat().st_mtime_ns, f.stat().st_size) for f in base.rglob("*.py"))
    return str(hash(tuple(partes)))


HUELLA_INICIO = huella_codigo()


def resolver_especie(texto: str) -> str:
    """Acepta el código SFC de la especie, el nemotécnico (ECOPETROL) o el nombre corto del emisor."""
    if q1("SELECT 1 AS x FROM dim_activo WHERE especie = ?", [texto]):
        return texto
    fila = q1("""SELECT a.especie FROM dim_activo a LEFT JOIN dim_emisor e USING (emisor_id)
                 WHERE upper(a.nemotecnico) = upper(?) OR lower(e.nombre_corto) = lower(?)
                 ORDER BY a.activa DESC, (a.clase_accion = 'ordinaria') DESC LIMIT 1""", [texto, texto])
    return fila["especie"] if fila else texto


@app.get("/api/estado")
def estado():
    precio = q1("SELECT max(fecha) AS f FROM fact_precio_diario")
    matriz = q1("SELECT max(fecha) AS f FROM fact_matriz")
    carteras = q("SELECT cartera_id, nombre, tipo, perfil_id FROM dim_cartera ORDER BY nombre")
    perfiles = q("SELECT perfil_id, max(version) AS version, any_value(nombre) AS nombre FROM dim_perfil GROUP BY 1 ORDER BY 3")
    return responder({"ultimo_precio": precio and precio["f"], "fecha_matriz": matriz and matriz["f"],
                      "carteras": carteras, "perfiles": perfiles, "base": str(ruta_base()),
                      # El código cambió desde que arrancó el servidor: hay que cerrarlo y volverlo a abrir
                      "reiniciar": huella_codigo() != HUELLA_INICIO})


@app.get("/api/activos")
def activos():
    filas = q("""
        SELECT a.especie, a.nemotecnico, a.nombre, a.clase_accion, a.activa, e.nombre_corto, e.sector_sfc
        FROM dim_activo a LEFT JOIN dim_emisor e USING (emisor_id)
        ORDER BY a.activa DESC, e.nombre_corto""")
    return responder(filas)


@app.get("/api/variables")
def variables():
    return responder(q("SELECT * FROM dim_variable ORDER BY categoria, nombre"))


# --------------------------------------------------------------------------- mercado
@app.get("/api/mercado")
def mercado():
    kpis = q("""
        WITH u AS (
            SELECT serie_id, fecha, valor, row_number() OVER (PARTITION BY serie_id ORDER BY fecha DESC) AS n
            FROM fact_macro WHERE serie_id IN ('trm_usd_cop','banrep_59','inflacion_co_12m','msci_colcap','fred_dgs10','fred_vixcls'))
        SELECT a.serie_id, d.corto, d.unidad, a.fecha, a.valor, b.valor AS anterior, b.fecha AS fecha_anterior
        FROM u a LEFT JOIN u b ON a.serie_id = b.serie_id AND b.n = 2
        LEFT JOIN dim_indicador_macro d ON d.serie_id = a.serie_id
        WHERE a.n = 1""")
    indice = q("""
        SELECT fecha, valor FROM fact_indice_mercado
        WHERE indice_id = 'universo_igual' AND moneda = 'COP' AND fecha >= (SELECT max(fecha) FROM fact_indice_mercado) - INTERVAL 365 DAY
        ORDER BY fecha""")
    mapa = q("""SELECT * FROM v_mapa_mercado WHERE capitalizacion IS NOT NULL ORDER BY capitalizacion DESC""")
    hoy = q1("SELECT max(fecha) AS f FROM fact_precio_diario WHERE precio_promedio IS NOT NULL")
    movimientos = []
    if hoy and hoy["f"]:
        movimientos = q("""
            SELECT p.especie, a.nemotecnico, e.nombre_corto, p.retorno_total, p.valor_transado,
                   (SELECT string_agg(left(ev.resumen, 140), ' · ') FROM fact_evento_corporativo ev
                    WHERE ev.emisor_id = p.emisor_id AND ev.fecha BETWEEN p.fecha - INTERVAL 3 DAY AND p.fecha) AS eventos
            FROM fact_precio_diario p
            JOIN fact_liquidez_diaria l ON l.especie = p.especie AND l.fecha = p.fecha AND l.liquida
            LEFT JOIN dim_activo a ON a.especie = p.especie LEFT JOIN dim_emisor e ON e.emisor_id = p.emisor_id
            WHERE p.fecha = ? AND p.retorno_total IS NOT NULL AND NOT coalesce(p.salto_atipico, false)
            ORDER BY abs(p.retorno_total) DESC LIMIT 8""", [hoy["f"]])
    eventos = q("""
        SELECT c.fecha, c.tipo, c.descripcion, c.valor, c.estimada, coalesce(e.nombre_corto, '') AS emisor
        FROM fact_evento_calendario c LEFT JOIN dim_emisor e USING (emisor_id)
        WHERE c.fecha >= current_date AND c.fecha <= current_date + INTERVAL 45 DAY
          AND c.tipo IN ('dividendo_ex', 'dividendo_pago', 'banrep_decision', 'dane_ipc')
        ORDER BY c.fecha LIMIT 40""")
    return responder({"kpis": kpis, "indice": indice, "mapa": mapa, "movimientos": movimientos,
                      "fecha_movimientos": hoy and hoy["f"], "eventos": eventos})


@app.get("/api/macro")
def macro_catalogo():
    return responder(q("SELECT * FROM dim_indicador_macro WHERE observaciones > 0 ORDER BY categoria, corto"))


@app.get("/api/macro/{serie_id}")
def macro_serie(serie_id: str, anios: int = 10):
    filas = q("""SELECT fecha, valor FROM fact_macro WHERE serie_id = ? AND fecha >= current_date - to_days(CAST(? AS INTEGER) * 365)
                 ORDER BY fecha""", [serie_id, anios])
    return responder(filas)


# --------------------------------------------------------------------------- screener y matriz
@app.get("/api/screener")
def screener():
    from invest.config import cargar_yaml

    fecha = q1("SELECT max(fecha) AS f FROM fact_matriz")
    if not fecha or not fecha["f"]:
        return responder({"filas": [], "screeners": []})
    filas = q("""SELECT m.*, a.nemotecnico, e.nombre_corto FROM fact_matriz m
                 LEFT JOIN dim_activo a USING (especie) LEFT JOIN dim_emisor e ON e.emisor_id = m.emisor_id
                 WHERE m.fecha = ? ORDER BY m.puntaje_global DESC NULLS LAST""", [fecha["f"]])
    corrida = q("SELECT screener_id, especie, nueva FROM fact_screener_corrida WHERE fecha = ?", [fecha["f"]])
    anterior = q1("SELECT max(fecha) AS f FROM fact_screener_corrida WHERE fecha < ?", [fecha["f"]])
    salieron = []
    if anterior and anterior["f"]:
        salieron = q("""SELECT a.screener_id, a.especie FROM fact_screener_corrida a
                        WHERE a.fecha = ? AND NOT EXISTS (SELECT 1 FROM fact_screener_corrida b
                          WHERE b.fecha = ? AND b.screener_id = a.screener_id AND b.especie = a.especie)""",
                     [anterior["f"], fecha["f"]])
    definiciones = cargar_yaml("screeners.yaml")["screeners"]
    screeners = [{"id": k, "nombre": v["nombre"], "descripcion": v["descripcion"],
                  "especies": [c["especie"] for c in corrida if c["screener_id"] == k],
                  "nuevas": [c["especie"] for c in corrida if c["screener_id"] == k and c["nueva"]],
                  "salieron": [s["especie"] for s in salieron if s["screener_id"] == k]} for k, v in definiciones.items()]
    return responder({"fecha": fecha["f"], "fecha_anterior": anterior and anterior["f"], "filas": filas,
                      "screeners": screeners})


@app.get("/api/matriz")
def matriz():
    filas = q("""SELECT especie, nemotecnico, emisor_corto, sector_sfc, en_universo, puntaje_fundamental,
                        puntaje_tecnico, puntaje_global, cuadrante, estado, decision, capitalizacion, tercio_validado
                 FROM v_ficha_actual WHERE puntaje_fundamental IS NOT NULL""")
    validacion = q("SELECT factor, veredicto, ic_medio, ic_t, exceso_superior_neto, exceso_superior_neto_t "
                   "FROM fact_validacion_factor ORDER BY veredicto DESC, factor")
    return responder({"filas": filas, "validacion": validacion})


# --------------------------------------------------------------------------- ficha
@app.get("/api/ficha/{especie}")
def ficha(especie: str):
    especie = resolver_especie(especie)
    cab = q1("""SELECT m.*, a.nemotecnico, a.nombre AS nombre_sfc, a.iba_categoria, a.clase_accion, e.nombre_corto,
                       e.nombre AS emisor_nombre, e.nit, e.moneda_estados
                FROM fact_matriz m LEFT JOIN dim_activo a USING (especie) LEFT JOIN dim_emisor e ON e.emisor_id = m.emisor_id
                WHERE m.especie = ? ORDER BY m.fecha DESC LIMIT 1""", [especie])
    if cab is None:
        act = q1("SELECT a.*, e.nombre_corto FROM dim_activo a LEFT JOIN dim_emisor e USING (emisor_id) WHERE especie = ?",
                 [especie])
        if act is None:
            raise HTTPException(404, "Especie desconocida")
        return responder({"cabecera": act, "sin_puntaje": True})
    fecha = cab["fecha"]
    auditoria = q("""SELECT p.elemento, p.nivel, p.factor, p.valor, p.z, p.puntaje, p.banda, p.percentil,
                            p.grupo_comparacion, p.pares, v.nombre AS variable, v.formato
                     FROM fact_puntaje p LEFT JOIN dim_variable v ON v.id = p.elemento
                     WHERE p.especie = ? AND p.fecha = ? ORDER BY p.nivel, p.factor, p.elemento""", [especie, fecha])
    tecnico = q1("SELECT * FROM fact_tecnico_diario WHERE especie = ? ORDER BY fecha DESC LIMIT 1", [especie])
    riesgo = q("""SELECT * FROM fact_riesgo WHERE especie = ? AND fecha = (SELECT max(fecha) FROM fact_riesgo WHERE especie = ?)
                  ORDER BY ventana, moneda""", [especie, especie])
    eventos = q("""SELECT fecha, categoria, tema, resumen FROM fact_evento_corporativo
                   WHERE emisor_id = ? ORDER BY fecha DESC, hora DESC LIMIT 12""", [cab["emisor_id"]])
    calendario = q("""SELECT fecha, tipo, descripcion, valor, moneda, estimada FROM fact_evento_calendario
                      WHERE emisor_id = ? AND fecha >= current_date - INTERVAL 400 DAY ORDER BY fecha DESC LIMIT 20""",
                   [cab["emisor_id"]])
    metricas = q("""SELECT metrica, base, fecha_corte, fecha_disponible, valor, unidad FROM fact_metrica_fundamental
                    WHERE emisor_id = ? ORDER BY fecha_corte DESC""", [cab["emisor_id"]])
    pares = q("""SELECT especie, nemotecnico, emisor_corto, precio, p_vl, p_u, roe_12m, rendimiento_dividendo_12m,
                        puntaje_global, en_universo
                 FROM v_ficha_actual WHERE sector_sfc = ? OR especie = ? ORDER BY capitalizacion DESC NULLS LAST""",
                 [cab["sector_sfc"], especie])
    avisos = q1("SELECT alertas FROM fact_ficha_accion WHERE especie = ?", [especie])
    return responder({"cabecera": cab, "auditoria": auditoria, "tecnico": tecnico, "riesgo": riesgo, "eventos": eventos,
                      "calendario": calendario, "metricas": metricas, "pares": pares,
                      "avisos": [a for a in ((avisos or {}).get("alertas") or "").split("; ") if a]})


METRICAS_BANCO_PARES = ["rote_12m", "roe_banco_12m", "nim_12m", "eficiencia_12m", "costo_riesgo_12m",
                         "indice_cartera_riesgo", "cobertura_cartera_cde", "crecimiento_cartera_12m"]
METRICAS_BANCO_SERIE = ["rote_12m", "roe_banco_12m", "nim_12m", "costo_riesgo_12m", "indice_cartera_riesgo"]


@app.get("/api/ficha/{especie}/banco")
def ficha_banco(especie: str):
    """Plantilla de bancos del emisor (gold/fact_metrica_banco): último corte, el de 12 meses antes, la
    serie de 5 años y los pares (una fila por entidad del CUIF). ``tiene: false`` si no es banco."""
    especie = resolver_especie(especie)
    from invest.config import cargar_yaml

    act = q1("SELECT emisor_id FROM dim_activo WHERE especie = ?", [especie])
    emisor = (act or {}).get("emisor_id")
    filas = q("""SELECT nombre_entidad, entidades, fecha_corte, fecha_disponible, metrica, valor, unidad, es_holding, base
                 FROM fact_metrica_banco WHERE emisor_id = ? ORDER BY fecha_corte""", [emisor]) if emisor else []
    if not filas:
        return responder({"tiene": False})
    ultimo = max(f["fecha_corte"] for f in filas)
    hace_un_anio = max((f["fecha_corte"] for f in filas
                        if (f["fecha_corte"].year, f["fecha_corte"].month) == (ultimo.year - 1, ultimo.month)), default=None)
    actual = {f["metrica"]: f["valor"] for f in filas if f["fecha_corte"] == ultimo}
    anterior = {f["metrica"]: f["valor"] for f in filas if f["fecha_corte"] == hace_un_anio}
    cab = next(f for f in filas if f["fecha_corte"] == ultimo)
    desde = date(ultimo.year - 5, ultimo.month, 1)
    serie: dict = {}
    for f in filas:
        if f["metrica"] in METRICAS_BANCO_SERIE and f["fecha_corte"] >= desde:
            serie.setdefault(f["fecha_corte"], {"fecha_corte": f["fecha_corte"]})[f["metrica"]] = f["valor"]
    lista = ", ".join("'" + m + "'" for m in METRICAS_BANCO_PARES)
    ultimos = q(f"""WITH u AS (SELECT entidades, max(fecha_corte) AS f FROM fact_metrica_banco GROUP BY entidades)
                   SELECT m.emisor_id, m.nombre_entidad, m.entidades, m.fecha_corte, m.metrica, m.valor, m.es_holding
                   FROM fact_metrica_banco m JOIN u ON u.entidades = m.entidades AND u.f = m.fecha_corte
                   WHERE m.metrica IN ({lista}) ORDER BY m.entidades, m.emisor_id""")
    pares: dict = {}
    for f in ultimos:
        # Dos emisores con el mismo banco (Davivienda y Davivienda Group) son un solo par: manda el de la ficha
        actual_par = pares.get(f["entidades"])
        if actual_par is None or (f["emisor_id"] == emisor and actual_par["emisor_id"] != emisor):
            actual_par = pares[f["entidades"]] = {"emisor_id": f["emisor_id"], "nombre_entidad": f["nombre_entidad"],
                                                  "entidades": f["entidades"], "fecha_corte": f["fecha_corte"],
                                                  "es_holding": f["es_holding"], "propio": f["emisor_id"] == emisor}
        if actual_par["emisor_id"] == f["emisor_id"]:
            actual_par[f["metrica"]] = f["valor"]
    try:
        nota = next((m.get("nota") for m in cargar_yaml("bancos.yaml")["mapeo"] if m.get("emisor_id") == emisor), None)
    except (OSError, KeyError):
        nota = None
    return responder({"tiene": True, "emisor_id": emisor, "nombre_entidad": cab["nombre_entidad"],
                      "entidades": cab["entidades"], "es_holding": cab["es_holding"], "base": cab["base"],
                      "fecha_corte": ultimo, "fecha_disponible": cab["fecha_disponible"], "corte_anterior": hace_un_anio,
                      "actual": actual, "anterior": anterior, "serie": sorted(serie.values(), key=lambda r: r["fecha_corte"]),
                      "pares": sorted(pares.values(), key=lambda r: -(r.get("rote_12m") or r.get("roe_banco_12m") or -9)),
                      "nota": nota})


@app.get("/api/ficha/{especie}/precio")
def ficha_precio(especie: str, dias: int = 365):
    especie = resolver_especie(especie)
    filas = q("""
        WITH s AS (
            SELECT fecha, precio_promedio, precio_ajustado, valor_transado,
                   avg(precio_ajustado) OVER (ORDER BY fecha ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma200,
                   count(*) OVER (ORDER BY fecha ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS n
            FROM v_precio_ajustado WHERE especie = ?)
        SELECT fecha, precio_promedio, precio_ajustado, valor_transado, CASE WHEN n = 200 THEN sma200 END AS sma200
        FROM s WHERE fecha >= (SELECT max(fecha) FROM s) - to_days(CAST(? AS INTEGER)) ORDER BY fecha""", [especie, dias])
    return responder(filas)


# --------------------------------------------------------------------------- cartera
@app.get("/api/cartera/{cartera_id}")
def cartera_vista(cartera_id: str):
    fecha = q1("SELECT max(fecha) AS f FROM fact_posicion WHERE cartera_id = ?", [cartera_id])
    if not fecha or not fecha["f"]:
        return responder({"vacia": True})
    f = fecha["f"]
    posiciones = q("""
        SELECT p.*, a.nemotecnico, e.nombre_corto, m.puntaje_global, m.cuadrante, t.retorno_12m
        FROM fact_posicion p LEFT JOIN dim_activo a USING (especie) LEFT JOIN dim_emisor e ON e.emisor_id = a.emisor_id
        LEFT JOIN v_ficha_actual m ON m.especie = p.especie
        LEFT JOIN (SELECT especie, retorno_12m, row_number() OVER (PARTITION BY especie ORDER BY fecha DESC) n
                   FROM fact_tecnico_diario) t ON t.especie = p.especie AND t.n = 1
        WHERE p.cartera_id = ? AND p.fecha = ? ORDER BY p.valor_cop DESC""", [cartera_id, f])
    asignacion = q("SELECT * FROM fact_asignacion WHERE cartera_id = ? AND fecha = ? ORDER BY clase_activo_id", [cartera_id, f])
    desempeno = q("SELECT * FROM fact_desempeno WHERE cartera_id = ? ORDER BY mes", [cartera_id])
    valor = q("""SELECT fecha, sum(valor_cop) AS valor FROM fact_posicion WHERE cartera_id = ? GROUP BY fecha ORDER BY fecha""",
              [cartera_id])
    especies = [p["especie"] for p in posiciones if p["especie"] != "EFECTIVO"]
    dividendos = []
    if especies:
        marcas = ", ".join("?" for _ in especies)
        dividendos = q(f"""
            SELECT c.fecha, c.tipo, c.valor, c.moneda, c.estimada, e.nombre_corto, p.cantidad, c.valor * p.cantidad AS total
            FROM fact_evento_calendario c JOIN dim_activo a ON a.emisor_id = c.emisor_id AND (c.especie IS NULL OR c.especie = a.especie)
            JOIN fact_posicion p ON p.especie = a.especie AND p.cartera_id = ? AND p.fecha = ?
            LEFT JOIN dim_emisor e ON e.emisor_id = c.emisor_id
            WHERE c.tipo = 'dividendo_pago' AND c.fecha >= current_date AND a.especie IN ({marcas})
            -- un mismo pago puede venir del aviso (por emisor) y del historial (por especie): uno por especie y día
            QUALIFY row_number() OVER (PARTITION BY c.fecha, a.especie ORDER BY c.especie NULLS LAST) = 1
            ORDER BY c.fecha""", [cartera_id, f, *especies])
    return responder({"fecha": f, "posiciones": posiciones, "asignacion": asignacion, "desempeno": desempeno,
                      "valor": valor, "dividendos": dividendos})


# --------------------------------------------------------------------------- datos del usuario
@app.get("/api/usuario/{tabla}")
def usuario_leer(tabla: str):
    if tabla not in usuario.TABLAS:
        raise HTTPException(404, "Tabla desconocida")
    return responder({"filas": usuario.leer(tabla).to_dicts(), "columnas": list(usuario.TABLAS[tabla]["columnas"]),
                      "claves": usuario.TABLAS[tabla]["claves"], "permitidos": usuario.TABLAS[tabla]["permitidos"],
                      "obligatorias": usuario.TABLAS[tabla]["obligatorias"]})


def _tipar(tabla: str, filas: list[dict]) -> pl.DataFrame:
    columnas = usuario.TABLAS[tabla]["columnas"]
    limpias = []
    for f in filas:
        fila = {}
        for c, t in columnas.items():
            v = f.get(c)
            if v == "" or v is None:
                fila[c] = None
            elif t == pl.Date:
                fila[c] = date.fromisoformat(str(v)[:10])
            elif t == pl.Float64:
                fila[c] = float(str(v).replace(",", "."))
            elif t == pl.Int64:
                fila[c] = int(float(v))
            else:
                fila[c] = str(v)
        limpias.append(fila)
    return pl.DataFrame(limpias, schema=columnas)


@app.post("/api/usuario/{tabla}")
def usuario_guardar(tabla: str, cuerpo: dict = Body(...)):
    """Agrega o reemplaza filas (por clave). ``{"filas": [...], "borrar": [{claves}]}``."""
    if tabla not in usuario.TABLAS:
        raise HTTPException(404, "Tabla desconocida")
    claves = usuario.TABLAS[tabla]["claves"]
    try:
        nuevas = _tipar(tabla, cuerpo.get("filas") or [])
    except (ValueError, TypeError) as e:
        return responder({"ok": False, "problemas": [f"dato inválido: {e}"]})
    if tabla == "fact_transaccion" and nuevas.height:
        nuevas = nuevas.with_columns(pl.when(pl.col("transaccion_id").is_null())
                                     .then(pl.Series([usuario.nuevo_id() for _ in range(nuevas.height)]))
                                     .otherwise(pl.col("transaccion_id")).alias("transaccion_id"))
    actual = usuario.leer(tabla)
    borrar = cuerpo.get("borrar") or []
    if borrar:
        quitar = _tipar(tabla, [{**{c: None for c in usuario.TABLAS[tabla]["columnas"]}, **b} for b in borrar]).select(claves)
        actual = actual.join(quitar, on=claves, how="anti")
    if nuevas.height:
        actual = actual.join(nuevas.select(claves), on=claves, how="anti")
    problemas = usuario.guardar(tabla, pl.concat([actual, nuevas], how="vertical_relaxed"))
    if problemas:
        return responder({"ok": False, "problemas": problemas})
    if tabla in ("fact_transaccion", "dim_cartera", "dim_perfil", "dim_perfil_asignacion"):
        _tras_cambio_usuario()
    return responder({"ok": True, "filas": usuario.leer(tabla).height})


# --------------------------------------------------------------------------- salud y acciones
@app.get("/api/salud")
def salud():
    fuentes = q("SELECT * FROM salud_datos ORDER BY estado DESC, fuente_id")
    validacion = q("SELECT * FROM fact_validacion_factor ORDER BY veredicto DESC, factor")
    archivo = data_dir() / "meta" / "ejecuciones.jsonl"
    ultimas = []
    if archivo.exists():
        lineas = deque(archivo.open(encoding="utf-8"), maxlen=40)
        for linea in reversed(lineas):
            try:
                ultimas.append(json.loads(linea))
            except json.JSONDecodeError:
                continue
    tablas = modelo.resumen_tablas().to_dicts()
    return responder({"fuentes": fuentes, "validacion": validacion, "ejecuciones": ultimas, "tablas": tablas})


@app.get("/api/salud/contrato")
def contrato():
    return responder({"problemas": modelo.verificar()})


ACCIONES = {
    "leer_bvc": ["ingest", "bvc_web", "bvc_indices"],  # lectura manual: la lanza Esteban con el botón
    "leer_estados_ventana": ["estados-ventana"],        # lectura manual con ventana de los sitios protegidos
    "actualizar_modelo": ["modelo"],
    "rehacer_base": ["modelo", "--sin-calcular"],
}
_trabajos: dict[str, dict] = {}


def _correr(trabajo_id: str, argumentos: list[str]) -> None:
    t = _trabajos[trabajo_id]
    try:
        entorno = dict(os.environ)
        if t["accion"] in ("leer_bvc", "leer_estados_ventana"):
            entorno["INVEST_LECTURA_MANUAL"] = "1"  # la pidió una persona desde el aplicativo
        proceso = subprocess.Popen([sys.executable, "-m", "invest", *argumentos], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                   stdin=subprocess.DEVNULL, env=entorno)
        for linea in proceso.stdout:  # type: ignore[union-attr]
            t["salida"].append(linea.rstrip())
        t["codigo"] = proceso.wait()
    except Exception as e:  # noqa: BLE001
        t["salida"].append(f"error: {e}")
        t["codigo"] = -1
    t["fin"] = datetime.now().isoformat(timespec="seconds")


@app.post("/api/acciones/{accion}")
def lanzar(accion: str):
    if accion not in ACCIONES:
        raise HTTPException(404, "Acción desconocida")
    if any(t["accion"] == accion and t["fin"] is None for t in _trabajos.values()):
        return responder({"ok": False, "problemas": ["ya hay una corriendo"]})
    trabajo_id = uuid.uuid4().hex[:8]
    _trabajos[trabajo_id] = {"accion": accion, "inicio": datetime.now().isoformat(timespec="seconds"), "fin": None,
                             "codigo": None, "salida": deque(maxlen=200)}
    threading.Thread(target=_correr, args=(trabajo_id, ACCIONES[accion]), daemon=True).start()
    return responder({"ok": True, "trabajo": trabajo_id})


@app.get("/api/acciones/{trabajo_id}")
def trabajo(trabajo_id: str):
    t = _trabajos.get(trabajo_id)
    if t is None:
        raise HTTPException(404, "Trabajo desconocido")
    return responder({**t, "salida": list(t["salida"])})


# --------------------------------------------------------------------------- páginas
@app.get("/")
def inicio():
    return FileResponse(ESTATICOS / "index.html")


@app.get("/tablero")
def tablero_validado():
    """El tablero de la matriz de factores validados (invest ficha)."""
    archivo = data_dir() / "tableros" / "matriz_factores.html"
    if not archivo.exists():
        raise HTTPException(404, "Corra invest ficha para generarlo")
    return FileResponse(archivo)


app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")


IDENTIDAD = "invest-pipeline"
PUERTO = 8790  # 8765 lo usa otro aplicativo de Esteban (seguimiento financiero SDIS)


@app.get("/api/identidad")
def identidad():
    """Permite reconocer, antes de arrancar, si lo que ocupa el puerto es este mismo aplicativo."""
    return responder({"app": IDENTIDAD})


def _responde(puerto: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", puerto)) == 0


def _es_este_aplicativo(puerto: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/identidad", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == IDENTIDAD
    except Exception:  # noqa: BLE001 - cualquier otra cosa en el puerto no es este aplicativo
        return False


def _libre(puerto: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", puerto))
            return True
        except OSError:
            return False


def elegir_puerto(preferido: int = PUERTO, intentos: int = 20) -> tuple[int, bool]:
    """(puerto, ya_abierto). Si el preferido lo ocupa otro programa, usa el siguiente libre.

    ``ya_abierto`` es True si este aplicativo ya está corriendo en ese puerto: entonces solo se abre
    el navegador, sin arrancar un segundo servidor.
    """
    for puerto in range(preferido, preferido + intentos):
        if _responde(puerto):
            if _es_este_aplicativo(puerto):
                return puerto, True
            continue  # otro aplicativo: se prueba el siguiente
        if _libre(puerto):
            return puerto, False
    raise RuntimeError(f"no hay un puerto libre entre {preferido} y {preferido + intentos - 1}")


def iniciar(puerto: int = PUERTO, abrir: bool = True) -> None:
    import uvicorn

    elegido, ya_abierto = elegir_puerto(puerto)
    url = f"http://127.0.0.1:{elegido}/"
    if elegido != puerto and not ya_abierto:
        print(f"El puerto {puerto} lo usa otro programa; el aplicativo queda en el {elegido}.")
    if ya_abierto:
        print(f"El aplicativo ya está abierto en {url}; se abre el navegador ahí.")
        if abrir:
            import webbrowser

            webbrowser.open(url)
        return
    asegurar_base()
    print(f"Aplicativo en {url} (cierre esta ventana o Ctrl+C para apagarlo)")
    if abrir:
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=elegido, log_level="warning")

