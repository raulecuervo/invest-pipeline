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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from invest import modelo, storage, usuario
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
    filas = q(f"""
        SELECT a.especie, a.nemotecnico, a.nombre, a.clase_accion, a.activa, e.nombre_corto, e.sector_sfc,
               {_sector()} AS sector
        FROM dim_activo a LEFT JOIN dim_emisor e USING (emisor_id)
        ORDER BY a.activa DESC, e.nombre_corto""")
    return responder(filas)


@app.get("/api/variables")
def variables():
    return responder(q("SELECT * FROM dim_variable ORDER BY categoria, nombre"))


# --------------------------------------------------------------------------- mercado
# --------------------------------------------------------------------------- precio en curso
def precios_en_curso(hoy: date | None = None) -> dict[str, dict]:
    """Última lectura en curso de hoy (silver/fact_precio_bvc_en_curso) por especie, solo para las especies
    que todavía no tienen precio de hoy en fact_precio_diario (el cierre manda siempre)."""
    from zoneinfo import ZoneInfo

    hoy = hoy or datetime.now(ZoneInfo("America/Bogota")).date()
    try:
        df = storage.leer_tabla("silver", "fact_precio_bvc_en_curso")
    except Exception:  # noqa: BLE001 - todavía no hay lecturas
        return {}
    if df is None or df.is_empty():
        return {}
    df = df.filter((pl.col("fecha") == hoy) & pl.col("especie").is_not_null() & pl.col("cierre").is_not_null())
    if df.is_empty():
        return {}
    con_cierre = {r["especie"] for r in q("SELECT DISTINCT especie FROM fact_precio_diario WHERE fecha = ?", [hoy])}
    salida = {}
    for r in df.iter_rows(named=True):
        if r["especie"] in con_cierre:
            continue
        salida[r["especie"]] = {
            "precio": r["cierre"], "variacion": r["variacion_pct"] / 100 if r["variacion_pct"] is not None else None,
            "volumen_cop": r["volumen_cop"], "nemotecnico": r["nemotecnico"],
            "hora": r["hora_lectura"].isoformat(timespec="minutes") if r["hora_lectura"] else None,
            "estado": r["estado_mercado"],
        }
    return salida


@app.get("/api/en_curso")
def en_curso():
    p = precios_en_curso()
    filas = sorted(({"especie": e, **v} for e, v in p.items()), key=lambda r: -(r["volumen_cop"] or 0))
    nombres = {r["especie"]: r["nombre_corto"] for r in q(
        "SELECT a.especie, e.nombre_corto FROM dim_activo a LEFT JOIN dim_emisor e USING (emisor_id)")} if filas else {}
    for r in filas:
        r["nombre_corto"] = nombres.get(r["especie"])
    hora = max((r["hora"] for r in filas if r["hora"]), default=None)
    return responder({"hora": hora, "estado": filas[0]["estado"] if filas else None, "precios": filas})


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
    mapa = q(f"""SELECT v.*, {_sector()} AS sector FROM v_mapa_mercado v
                 LEFT JOIN dim_activo a ON a.especie = v.especie LEFT JOIN dim_emisor e ON e.emisor_id = a.emisor_id
                 WHERE v.capitalizacion IS NOT NULL ORDER BY v.capitalizacion DESC""")
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
        SELECT c.fecha, c.tipo, c.descripcion, c.valor, c.moneda, c.estimada, coalesce(e.nombre_corto, '') AS emisor,
               c.especie, a.nemotecnico, a.clase_accion
        FROM fact_evento_calendario c LEFT JOIN dim_emisor e USING (emisor_id) LEFT JOIN dim_activo a ON a.especie = c.especie
        WHERE c.fecha >= current_date AND c.fecha <= current_date + INTERVAL 45 DAY
          AND c.tipo IN ('dividendo_ex', 'dividendo_pago', 'banrep_decision', 'dane_ipc')
        ORDER BY c.fecha LIMIT 40""")
    return responder({"kpis": kpis, "indice": indice, "mapa": mapa, "movimientos": movimientos,
                      "fecha_movimientos": hoy and hoy["f"], "eventos": eventos})


@app.get("/api/macro")
def macro_catalogo():
    return responder(q("SELECT * FROM dim_indicador_macro WHERE observaciones > 0 ORDER BY categoria, corto"))


@app.get("/api/macro/{serie_id}")
def macro_serie(serie_id: str, anios: int = 10, dias: int | None = None):
    """Serie macro de los últimos ``anios`` años, o de los últimos ``dias`` días si se dan (periodos cortos)."""
    dias = dias if dias is not None else anios * 365
    filas = q("""SELECT fecha, valor FROM fact_macro WHERE serie_id = ? AND fecha >= current_date - to_days(CAST(? AS INTEGER))
                 ORDER BY fecha""", [serie_id, dias])
    return responder(filas)


@app.get("/api/indice_universo")
def indice_universo(dias: int = 365, moneda: str = "COP"):
    """Índice de pesos iguales del universo líquido en los últimos ``dias`` días (gráfico de Mercado)."""
    filas = q("""SELECT fecha, valor FROM fact_indice_mercado
                 WHERE indice_id = 'universo_igual' AND moneda = ?
                   AND fecha >= (SELECT max(fecha) FROM fact_indice_mercado) - to_days(CAST(? AS INTEGER))
                 ORDER BY fecha""", [moneda, dias])
    return responder(filas)


# --------------------------------------------------------------------------- screener y matriz
@app.get("/api/screener")
def screener():
    from invest.config import cargar_yaml

    fecha = q1("SELECT max(fecha) AS f FROM fact_matriz")
    if not fecha or not fecha["f"]:
        return responder({"filas": [], "screeners": []})
    # EV/EBITDA y margen EBITDA: solo si la base ya se reconstruyó con ellos (invest modelo)
    if _graf_tabla_existe("fact_valoracion", ("ev_ebitda", "deuda_neta_ebitda")) and \
            _graf_tabla_existe("fact_metrica_fundamental", ("metrica", "valor", "fecha_corte", "fecha_disponible")):
        filas = q(f"""SELECT m.*, a.nemotecnico, e.nombre_corto, {_sector()} AS sector, v.ev_ebitda, v.deuda_neta_ebitda,
                            mg.margen_ebitda_12m
                     FROM fact_matriz m
                     LEFT JOIN dim_activo a USING (especie) LEFT JOIN dim_emisor e ON e.emisor_id = m.emisor_id
                     LEFT JOIN (SELECT especie, ev_ebitda, deuda_neta_ebitda FROM fact_valoracion
                                WHERE fecha = (SELECT max(fecha) FROM fact_valoracion WHERE fecha <= ?)) v USING (especie)
                     LEFT JOIN (SELECT emisor_id, arg_max(valor, fecha_corte) AS margen_ebitda_12m FROM fact_metrica_fundamental
                                WHERE metrica = 'margen_ebitda_12m' AND base = 'consolidado' AND fecha_disponible <= ?
                                  AND fecha_corte >= CAST(? AS DATE) - INTERVAL 460 DAY
                                GROUP BY emisor_id) mg ON mg.emisor_id = m.emisor_id
                     WHERE m.fecha = ? ORDER BY m.puntaje_global DESC NULLS LAST""", [fecha["f"]] * 4)
    else:
        filas = q(f"""SELECT m.*, a.nemotecnico, e.nombre_corto, {_sector()} AS sector FROM fact_matriz m
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
    filas = q(f"""SELECT f.especie, f.nemotecnico, f.emisor_corto, f.sector_sfc, {_sector()} AS sector, f.en_universo,
                        f.puntaje_fundamental, f.puntaje_tecnico, f.puntaje_global, f.cuadrante, f.estado, f.decision,
                        f.capitalizacion, f.tercio_validado
                 FROM v_ficha_actual f LEFT JOIN dim_emisor e ON e.emisor_id = f.emisor_id
                 WHERE f.puntaje_fundamental IS NOT NULL""")
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
    # El puntaje es del último día de la SFC; si la Bolsa ya cerró días después, la cabecera muestra ese cierre
    ultimo = q1("""SELECT fecha, precio, fuente_precio FROM fact_precio_diario
                   WHERE especie = ? AND precio IS NOT NULL ORDER BY fecha DESC LIMIT 1""", [especie])
    if ultimo and ultimo["fecha"] > fecha:
        cab["precio_ultimo"], cab["fecha_ultimo"], cab["fuente_ultimo"] = ultimo["precio"], ultimo["fecha"], ultimo["fuente_precio"]
    explica = "p.mediana, p.escala, p.direccion" if _graf_tabla_existe("fact_puntaje", ("mediana", "escala", "direccion")) \
        else "NULL AS mediana, NULL AS escala, NULL AS direccion"
    auditoria = q(f"""SELECT p.elemento, p.nivel, p.factor, p.valor, p.z, p.puntaje, p.banda, p.percentil,
                            p.grupo_comparacion, p.pares, {explica}, v.nombre AS variable, v.formato
                     FROM fact_puntaje p LEFT JOIN dim_variable v ON v.id = p.elemento
                     WHERE p.especie = ? AND p.fecha = ? ORDER BY p.nivel, p.factor, p.elemento""", [especie, fecha])
    tecnico = q1("SELECT * FROM fact_tecnico_diario WHERE especie = ? ORDER BY fecha DESC LIMIT 1", [especie])
    riesgo = q("""SELECT * FROM fact_riesgo WHERE especie = ? AND fecha = (SELECT max(fecha) FROM fact_riesgo WHERE especie = ?)
                  ORDER BY ventana, moneda""", [especie, especie])
    eventos = q("""SELECT fecha, categoria, tema, resumen FROM fact_evento_corporativo
                   WHERE emisor_id = ? ORDER BY fecha DESC, hora DESC LIMIT 12""", [cab["emisor_id"]])
    # Los eventos del emisor (asambleas, avisos sin especie) y los de esta especie, no los de sus otras clases
    calendario = q("""SELECT fecha, tipo, descripcion, valor, moneda, estimada FROM fact_evento_calendario
                      WHERE emisor_id = ? AND (especie IS NULL OR especie = ?)
                        AND fecha >= current_date - INTERVAL 400 DAY ORDER BY fecha DESC LIMIT 20""",
                   [cab["emisor_id"], especie])
    metricas = q("""SELECT metrica, base, fecha_corte, fecha_disponible, valor, unidad FROM fact_metrica_fundamental
                    WHERE emisor_id = ? ORDER BY fecha_corte DESC""", [cab["emisor_id"]])
    ev = "f.ev_ebitda, f.deuda_neta_ebitda" if _graf_tabla_existe("v_ficha_actual", ("ev_ebitda", "deuda_neta_ebitda")) \
        else "NULL AS ev_ebitda, NULL AS deuda_neta_ebitda"
    # Pares: las acciones del mismo sector por negocio (config/sectores.yaml)
    sector = _sector()
    cab["sector"] = (q1(f"SELECT {sector} AS s FROM dim_emisor e WHERE e.emisor_id = ?", [cab["emisor_id"]]) or {}).get("s")
    pares = q(f"""SELECT f.especie, f.nemotecnico, f.emisor_corto, f.precio, f.p_vl, f.p_u, f.roe_12m,
                        f.rendimiento_dividendo_12m, {ev},
                        f.puntaje_global, f.en_universo
                 FROM v_ficha_actual f LEFT JOIN dim_emisor e ON e.emisor_id = f.emisor_id
                 WHERE {sector} = ? OR f.especie = ? ORDER BY f.capitalizacion DESC NULLS LAST""",
                 [cab["sector"], especie])
    avisos = q1("SELECT alertas FROM fact_ficha_accion WHERE especie = ?", [especie])
    ahora = precios_en_curso().get(especie)
    if ahora:
        propia = next((p for p in pares if p["especie"] == especie), None) or {}
        factor = ahora["precio"] / propia["precio"] if propia.get("precio") else None
        ahora = {**ahora, "p_vl": propia.get("p_vl") * factor if factor and propia.get("p_vl") else None,
                 "p_u": propia.get("p_u") * factor if factor and propia.get("p_u") else None}
    return responder({"cabecera": cab, "en_curso": ahora, "auditoria": auditoria, "escala": _escala_puntaje(), "tecnico": tecnico, "riesgo": riesgo, "eventos": eventos,
                      "calendario": calendario, "metricas": metricas, "pares": pares,
                      "avisos": [a for a in ((avisos or {}).get("alertas") or "").split("; ") if a]})


def _escala_puntaje() -> dict:
    """Parámetros de config/escala.yaml que la ficha necesita para explicar cada puntaje."""
    from invest import puntajes
    cfg = puntajes.config()
    e = cfg["escala"]
    return {"centro": e["centro"], "pendiente": e["pendiente"], "z_max": e["z_max"], "minimo_pares": e["minimo_pares"],
            "minimo_pares_metrica": e.get("minimo_pares_metrica", 5), "minimo_factores_global": e.get("minimo_factores_global", 1),
            "minimo_bancos": (cfg.get("bancos") or {}).get("minimo_pares"),
            "pesos": {f: d.get("peso", 1) for f, d in cfg["factores"].items()},
            "fundamental": cfg["matriz"]["fundamental"], "tecnico": cfg["matriz"]["tecnico"],
            "factores": {f: [{"metrica": m, "direccion": md.get("direccion", 1)} for m, md in d["metricas"].items()]
                         for f, d in cfg["factores"].items()},
            "bancos": {f: [{"metrica": m, "direccion": md.get("direccion", 1), "respaldo": md.get("respaldo")}
                           for m, md in d["metricas"].items()]
                       for f, d in ((cfg.get("bancos") or {}).get("factores") or {}).items()}}


@app.get("/api/escala_puntaje")
def escala_puntaje():
    return responder(_escala_puntaje())


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
    """Serie diaria del precio ajustado de los últimos ``dias`` días con sus medias móviles (simples de 20,
    50, 100 y 200 sesiones y exponenciales de 20 y 50, calculadas sobre toda la historia para que no
    arranquen vacías) y, desde que hay lectura de la Bolsa, apertura, máximo, mínimo y cierre del día
    llevados a la misma escala del precio ajustado (velas).

    La serie ajustada sale de la SFC, que publica con uno o dos días hábiles de rezago. Los días que la
    Bolsa ya cerró y la SFC todavía no publica se agregan al final con ``provisional = true``: el cierre
    de la Bolsa llevado a la escala ajustada con el factor del último día de la SFC (si hubo una fecha ex
    en medio, la cifra se corrige sola cuando llega la SFC)."""
    especie = resolver_especie(especie)
    hay_ohlc = _graf_tabla_existe("fact_precio_diario", ("apertura_bvc", "maximo_bvc", "minimo_bvc", "cierre_bvc"))
    ohlc = "p.apertura_bvc, p.maximo_bvc, p.minimo_bvc, p.cierre_bvc" if hay_ohlc \
        else "NULL AS apertura_bvc, NULL AS maximo_bvc, NULL AS minimo_bvc, NULL AS cierre_bvc"
    filas = q(f"""SELECT v.fecha, v.precio_promedio, v.precio_ajustado, v.valor_transado, {ohlc}
                  FROM v_precio_ajustado v
                  LEFT JOIN fact_precio_diario p ON p.especie = v.especie AND p.fecha = v.fecha
                  WHERE v.especie = ? ORDER BY v.fecha""", [especie])
    for f in filas:
        f["factor"] = (f["precio_ajustado"] / f["precio_promedio"]) if f["precio_promedio"] else None
        f["provisional"] = False
    if filas and hay_ohlc:
        factor = next((f["factor"] for f in reversed(filas) if f["factor"]), None)
        nuevos = q("""SELECT fecha, precio, valor_transado, apertura_bvc, maximo_bvc, minimo_bvc, cierre_bvc
                      FROM fact_precio_diario WHERE especie = ? AND fecha > ? AND precio IS NOT NULL
                      ORDER BY fecha""", [especie, filas[-1]["fecha"]])
        for n in nuevos if factor else []:
            filas.append({"fecha": n["fecha"], "precio_promedio": None, "precio_ajustado": n["precio"] * factor,
                          "valor_transado": n["valor_transado"], "apertura_bvc": n["apertura_bvc"],
                          "maximo_bvc": n["maximo_bvc"], "minimo_bvc": n["minimo_bvc"], "cierre_bvc": n["cierre_bvc"],
                          "factor": factor, "provisional": True})
    precios = [f["precio_ajustado"] for f in filas]
    medias = {f"sma{n}": _media_simple(precios, n) for n in MEDIAS_SIMPLES}
    medias |= {f"ema{n}": _media_exponencial(precios, n) for n in MEDIAS_EXPONENCIALES}
    for i, f in enumerate(filas):
        for k, serie in medias.items():
            f[k] = serie[i]
        # La Bolsa da precios del día sin ajustar: se llevan a la escala del precio ajustado
        factor = f.pop("factor")
        for k in ("apertura", "maximo", "minimo", "cierre"):
            v = f.pop(f"{k}_bvc")
            f[k] = v * factor if v is not None and factor is not None else None
    if filas:
        desde = filas[-1]["fecha"] - timedelta(days=int(dias))
        filas = [f for f in filas if f["fecha"] >= desde]
    return responder(filas)


MEDIAS_SIMPLES = (20, 50, 100, 200)
MEDIAS_EXPONENCIALES = (20, 50)


def _media_simple(xs: list, n: int) -> list:
    """Promedio de las últimas ``n`` sesiones; vacío hasta tener ``n`` (o si falta un precio en la ventana)."""
    salida, suma, nulos = [], 0.0, 0
    for i, x in enumerate(xs):
        suma += x or 0.0
        nulos += x is None
        if i >= n:
            suma -= xs[i - n] or 0.0
            nulos -= xs[i - n] is None
        salida.append(suma / n if i >= n - 1 and nulos == 0 else None)
    return salida


def _media_exponencial(xs: list, n: int) -> list:
    """Media exponencial con alfa = 2 / (n + 1), sembrada con la media simple de las primeras ``n``."""
    alfa, salida, e, vistos = 2 / (n + 1), [], None, []
    for x in xs:
        if x is None:
            salida.append(e if len(vistos) >= n else None)
            continue
        if len(vistos) < n:
            vistos.append(x)
            e = sum(vistos) / n if len(vistos) == n else None
        else:
            e = alfa * x + (1 - alfa) * e
        salida.append(e)
    return salida


# =========================================================================== Documentos
@app.get("/api/documentos")
def documentos_lista():
    """Catálogo de los PDF y anexos descargados, con el nombre del emisor y su especie principal."""
    from invest import documentos
    filas = documentos.catalogo().to_dicts()
    nombres = {r["emisor_id"]: r for r in q("SELECT * FROM dim_emisor")}
    activos = q("SELECT emisor_id, nemotecnico, especie FROM dim_activo ORDER BY activa DESC, especie")
    por_especie = {r["especie"]: r["nemotecnico"] for r in activos}
    por_emisor: dict[str, str] = {}
    for r in activos:
        por_emisor.setdefault(r["emisor_id"], r["nemotecnico"])
    for f in filas:
        e = nombres.get(f["emisor_id"]) or {}
        f["emisor"] = e.get("nombre_corto") or e.get("nombre") or f["emisor_id"] or "Sin emisor identificado"
        f["nemotecnico"] = por_especie.get(e.get("especie_principal")) or por_emisor.get(f["emisor_id"])
    return responder(filas)


@app.get("/api/documentos/archivo")
def documentos_archivo(ruta: str):
    """El archivo para verlo en el navegador; de un ZIP, el PDF de los estados que trae adentro."""
    from invest import documentos
    from invest.ingest import estados_compania as ec
    try:
        archivo = documentos.ruta_segura(ruta)
    except ValueError:
        raise HTTPException(400, "Archivo no permitido") from None
    contenido = archivo.read_bytes()
    nombre = archivo.name
    if archivo.suffix.lower() == ".zip":
        pdf = ec.pdf_de_zip(contenido)
        if pdf is not None:
            contenido, nombre = pdf, archivo.stem + ".pdf"
    tipos = {".pdf": "application/pdf", ".zip": "application/zip",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    tipo = tipos.get(Path(nombre).suffix.lower(), "application/octet-stream")
    disposicion = "inline" if tipo == "application/pdf" else "attachment"
    from urllib.parse import quote
    return Response(contenido, media_type=tipo,
                    headers={"Content-Disposition": f"{disposicion}; filename*=UTF-8''{quote(nombre)}"})


# =========================================================================== Revisión de estados
# Las cifras en que los dos lectores y el árbitro no se pusieron de acuerdo (invest-data/revision/
# estados_compania.csv): la página del PDF con las cifras resaltadas y la decisión de Esteban.
DECISIONES_REVISION = ("a", "b", "corregido", "rechazado", "")


def _filas_revision_estados() -> list[dict]:
    import csv

    from invest.ingest import estados_compania as ec
    ruta = ec.ruta_revision()
    if not ruta.exists():
        return []
    with open(ruta, encoding="utf-8-sig", errors="replace", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


@app.get("/api/revision_estados")
def revision_estados():
    return responder(_filas_revision_estados())


@app.get("/api/revision_estados/sin_leer")
def revision_sin_leer():
    """Documentos que no se pudieron leer (y los que ya tienen decisión), de revision/documentos_sin_leer.csv."""
    from invest.ingest import estados_compania as ec
    return responder(ec.leer_sin_leer())


@app.post("/api/revision_estados/sin_leer")
def revision_sin_leer_guardar(cuerpo: dict = Body(...)):
    """decision: sin_estados (no trae estados: no se reintenta), paginas (con «paginas», p. ej. 5-8: la
    siguiente lectura envía esas) o vacío para deshacer."""
    from invest.ingest import estados_compania as ec
    decision = str(cuerpo.get("decision") or "").strip().lower()
    if decision and decision not in ec.DECISIONES_SIN_LEER:
        raise HTTPException(400, "Decisión no permitida")
    paginas = str(cuerpo.get("paginas") or "").strip()
    filas = ec.leer_sin_leer()
    fila = next((f for f in filas if f.get("documento_id") == cuerpo.get("documento_id")), None)
    if fila is None:
        raise HTTPException(404, "Documento desconocido")
    if decision == "paginas":
        try:
            indices = ec.paginas_de_texto(paginas)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        total = int(float(fila.get("paginas_total") or 0))
        if total and indices[-1] >= total:
            raise HTTPException(400, f"El documento tiene {total} páginas")
        paginas = ", ".join(_rangos([i + 1 for i in indices]))
    fila["decision"], fila["paginas"] = decision, paginas if decision == "paginas" else ""
    try:
        ec.guardar_sin_leer(filas)
    except PermissionError:
        raise HTTPException(409, "El archivo revision/documentos_sin_leer.csv está abierto en otro programa "
                                 "(¿Excel?). Ciérrelo y vuelva a intentar.") from None
    return responder({"ok": True, "fila": fila})


def _rangos(numeros: list[int]) -> list[str]:
    """[4, 5, 6, 9] → ["4-6", "9"]."""
    salida: list[str] = []
    for n in sorted(set(numeros)):
        if salida and int(salida[-1].split("-")[-1]) == n - 1:
            salida[-1] = f"{salida[-1].split('-')[0]}-{n}"
        else:
            salida.append(str(n))
    return salida


def _pdf_de_revision(archivo: str) -> bytes:
    """El PDF (o los PDF unidos) de una fila de revisión; solo archivos dentro de la carpeta de datos."""
    from invest.config import data_dir
    from invest.ingest import estados_compania as ec
    base = data_dir().resolve()
    partes = []
    for rel in [x for x in (archivo or "").split(";") if x]:
        ruta = (base / rel).resolve()
        if base not in ruta.parents or ruta.suffix.lower() != ".pdf" or not ruta.exists():
            raise HTTPException(400, "Archivo no permitido")
        partes.append(ruta.read_bytes())
    if not partes:
        raise HTTPException(400, "Sin archivo")
    return partes[0] if len(partes) == 1 else ec.unir_pdf(partes)


def variantes_cifra(texto: str) -> list[str]:
    """Cómo puede estar impresa una cifra: 637979587 → 637,979,587 y 637.979.587; 2705.93 → 2,705.93 y 2.705,93."""
    try:
        v = abs(float(texto))
    except (TypeError, ValueError):
        return []
    if v == int(v):
        coma = f"{int(v):,}"
        salida = [coma, coma.replace(",", ".")]
    else:
        coma = f"{v:,.2f}"
        salida = [coma, coma.replace(",", "X").replace(".", ",").replace("X", ".")]
    return list(dict.fromkeys(x for x in salida if len(x) >= 3))


COLORES_REVISION = [(245, 181, 74), (76, 201, 240), (180, 140, 255)]  # A, B, árbitro (como en la página)
_PDFIUM = threading.Lock()  # pdfium no admite dos hilos a la vez (la página pide dos imágenes juntas)


@app.get("/api/revision_estados/imagen")
def revision_imagen(archivo: str, pagina: int, valores: str = "", recorte: int = 1):
    """PNG de la página con las cifras resaltadas (A ámbar, B azul, árbitro violeta). Con ``recorte`` se
    corta a la franja de la página donde están las cifras."""
    with _PDFIUM:
        return _imagen_revision(archivo, pagina, valores, recorte)


def _imagen_revision(archivo: str, pagina: int, valores: str, recorte: int):
    import io

    import pypdfium2 as pdfium
    from PIL import ImageDraw
    pdf = pdfium.PdfDocument(_pdf_de_revision(archivo))
    if not 1 <= pagina <= len(pdf):
        raise HTTPException(404, "Página fuera del documento")
    hoja = pdf[pagina - 1]
    escala = 2
    imagen = hoja.render(scale=escala).to_pil().convert("RGB")
    _, alto = hoja.get_size()
    texto = hoja.get_textpage()
    dibujo = ImageDraw.Draw(imagen, "RGBA")
    marcas = []
    for k, valor in enumerate(valores.split(",") if valores else []):
        color = COLORES_REVISION[k % len(COLORES_REVISION)]
        for variante in variantes_cifra(valor):
            busqueda = texto.search(variante, match_case=False, match_whole_word=False)
            while (hallado := busqueda.get_next()):
                i, n = hallado
                cajas = [texto.get_charbox(j) for j in range(i, i + n)]
                x0, y0 = min(c[0] for c in cajas), min(c[1] for c in cajas)
                x1, y1 = max(c[2] for c in cajas), max(c[3] for c in cajas)
                rect = [x0 * escala - 5, (alto - y1) * escala - 5, x1 * escala + 5, (alto - y0) * escala + 5]
                dibujo.rectangle(rect, fill=(*color, 70), outline=(*color, 255), width=4)
                marcas.append(rect)
    if recorte and marcas:
        arriba = max(0, min(m[1] for m in marcas) - 420)
        abajo = min(imagen.height, max(m[3] for m in marcas) + 420)
        imagen = imagen.crop((0, int(arriba), imagen.width, int(abajo)))
    salida = io.BytesIO()
    imagen.save(salida, format="PNG", optimize=True)
    return Response(salida.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/revision_estados")
def revision_guardar(cuerpo: dict = Body(...)):
    """Guarda la decisión de una fila (a, b, corregido con valor_revisado, rechazado, o vacío para deshacer)."""
    import csv

    from invest.ingest import estados_compania as ec
    decision = str(cuerpo.get("revision") or "").strip().lower()
    if decision not in DECISIONES_REVISION:
        raise HTTPException(400, "Decisión no permitida")
    valor = str(cuerpo.get("valor_revisado") or "").strip()
    if decision == "corregido" and ec._a_numero(valor) is None:
        raise HTTPException(400, "Falta la cifra corregida")
    filas = _filas_revision_estados()
    fila = next((f for f in filas if f.get("documento_id") == cuerpo.get("documento_id")
                 and f.get("concepto") == cuerpo.get("concepto")), None)
    if fila is None:
        raise HTTPException(404, "Fila desconocida")
    fila["revision"], fila["valor_revisado"] = decision, valor if decision == "corregido" else ""
    fila["notas_revision"] = str(cuerpo.get("notas_revision") or fila.get("notas_revision") or "")[:500]
    try:
        with open(ec.ruta_revision(), "w", encoding="utf-8-sig", newline="") as f:
            escritor = csv.DictWriter(f, fieldnames=ec.COLUMNAS_REVISION, delimiter=";", extrasaction="ignore")
            escritor.writeheader()
            escritor.writerows(filas)
    except PermissionError:  # Windows: el archivo abierto en Excel no se deja escribir
        raise HTTPException(409, "El archivo revision/estados_compania.csv está abierto en otro programa "
                                 "(¿Excel?). Ciérrelo y vuelva a elegir.") from None
    return responder({"ok": True, "fila": fila})


# =========================================================================== Valoración (bandas)
# Bandas de valoración (src/invest/bandas.py): P/VL y P/U de cada acción frente a su propia historia.
BANDAS_INDICADORES = {"p_vl": "P/VL", "p_u": "P/U"}


def _config_bandas() -> dict:
    from invest.config import cargar_yaml

    try:
        return cargar_yaml("bandas.yaml") or {}
    except OSError:
        return {}


def _parametros_bandas(indicador: str, ventana: str) -> tuple[str, str, dict]:
    cfg = _config_bandas()
    ventanas = [str(v) for v in (cfg.get("ventanas") or {"1a": 1, "3a": 3, "5a": 5, "10a": 10})]
    if indicador not in BANDAS_INDICADORES:
        raise HTTPException(400, f"Indicador desconocido: {indicador} (use {', '.join(BANDAS_INDICADORES)})")
    if ventana not in ventanas:
        raise HTTPException(400, f"Ventana desconocida: {ventana} (use {', '.join(ventanas)})")
    return indicador, ventana, {**cfg, "_ventanas": ventanas}


def _contexto_bandas(cfg: dict) -> dict:
    return {"indicadores": [{"id": k, "nombre": v} for k, v in BANDAS_INDICADORES.items()],
            "ventanas": cfg["_ventanas"], "cobertura_minima": cfg.get("cobertura_minima", 0.6),
            "recortar_al_quiebre": bool(cfg.get("recortar_al_quiebre", False))}


@app.get("/api/valoracion/bandas")
def valoracion_bandas(indicador: str = "p_vl", ventana: str = "5a"):
    """Todas las especies con bandas al último día para un indicador y una ventana."""
    indicador, ventana, cfg = _parametros_bandas(indicador, ventana)
    sector = _sector()
    filas = q(f"""
        SELECT b.especie, a.nemotecnico, e.nombre_corto, a.nombre AS nombre_sfc,
               coalesce(a.sector_sfc, e.sector_sfc) AS sector_sfc, {sector} AS sector, coalesce(m.en_universo, false) AS en_universo,
               b.fecha, b.actual, b.fecha_actual, b.media, b.desviacion, b.z, b.percentil, b.minimo, b.maximo,
               b.n_obs, b.sesiones, b.fecha_desde, b.cobertura, b.suficiente, b.quiebre, b.tipo_quiebre,
               b.recortada, b.aviso
        FROM fact_banda_valoracion b
        LEFT JOIN dim_activo a ON a.especie = b.especie
        LEFT JOIN dim_emisor e ON e.emisor_id = b.emisor_id
        LEFT JOIN v_ficha_actual m ON m.especie = b.especie
        WHERE b.indicador = ? AND b.ventana = ?
        ORDER BY b.z NULLS LAST""", [indicador, ventana])
    if not filas:  # sin v_ficha_actual (antes de los puntajes) la consulta de arriba no corre
        filas = q(f"""SELECT b.*, a.nemotecnico, e.nombre_corto, a.nombre AS nombre_sfc,
                            coalesce(a.sector_sfc, e.sector_sfc) AS sector_sfc, {sector} AS sector, false AS en_universo
                     FROM fact_banda_valoracion b LEFT JOIN dim_activo a ON a.especie = b.especie
                     LEFT JOIN dim_emisor e ON e.emisor_id = b.emisor_id
                     WHERE b.indicador = ? AND b.ventana = ? ORDER BY b.z NULLS LAST""", [indicador, ventana])
    fecha = max((f["fecha"] for f in filas), default=None)
    return responder({"fecha": fecha, "indicador": indicador, "ventana": ventana, "filas": filas,
                      **_contexto_bandas(cfg)})


@app.get("/api/ficha/{especie}/bandas")
def ficha_bandas(especie: str, indicador: str = "p_vl", ventana: str = "5a"):
    """Serie diaria del indicador en la ventana, con la media y las bandas (constantes en la ventana) y
    los quiebres. ``tiene: false`` si la especie no tiene bandas para esa combinación."""
    especie = resolver_especie(especie)
    indicador, ventana, cfg = _parametros_bandas(indicador, ventana)
    resumen = q("""SELECT indicador, ventana, anios, z, percentil, cobertura, suficiente, quiebre
                   FROM fact_banda_valoracion WHERE especie = ? ORDER BY indicador, anios""", [especie])
    banda = q1("SELECT * FROM fact_banda_valoracion WHERE especie = ? AND indicador = ? AND ventana = ?",
               [especie, indicador, ventana])
    base = {"especie": especie, "indicador": indicador, "nombre_indicador": BANDAS_INDICADORES[indicador],
            "ventana": ventana, "resumen": resumen, **_contexto_bandas(cfg)}
    if banda is None:
        return responder({**base, "tiene": False})
    # El indicador del día de cada fila: con cifras por acción vigentes ese día (as-of)
    serie = q(f"""SELECT fecha, {indicador} AS valor FROM fact_valoracion_diaria
                  WHERE especie = ? AND fecha BETWEEN ? AND ? ORDER BY fecha""",
              [especie, banda["fecha_desde"], banda["fecha"]])
    quiebres = q("""SELECT fecha, tipo FROM fact_quiebre_valoracion
                    WHERE especie = ? AND (indicador IS NULL OR indicador = ?) AND fecha BETWEEN ? AND ?
                    ORDER BY fecha""", [especie, indicador, banda["fecha_desde"], banda["fecha"]])
    return responder({**base, "tiene": True, "banda": banda, "serie": serie, "quiebres": quiebres})
# =========================================================================== fin Valoración (bandas)


# --------------------------------------------------------------------------- cartera
@app.get("/api/alertas")
def alertas_vista():
    """Alertas automáticas y propias, calculadas al momento (src/invest/alertas.py)."""
    from invest import alertas

    asegurar_base()
    with duckdb.connect(str(ruta_base()), read_only=True) as con:
        filas = alertas.calcular(con)
    reglas = usuario.leer("dim_alerta").to_dicts() if (usuario.carpeta() / "dim_alerta.parquet").exists() else []
    return responder({"alertas": filas, "reglas": reglas, "condiciones": {k: v[2] for k, v in alertas.CONDICIONES.items()}})


@app.get("/api/cartera/{cartera_id}/rebalanceo")
def cartera_rebalanceo(cartera_id: str, aporte: float = 0.0, modo: str = "completo", comision: float = 0.0):
    """Montos por clase y por posición para volver a la asignación objetivo del perfil (aritmética, no
    recomendación). ``comision`` en fracción (0,003 = 0,3 %)."""
    from invest import rebalanceo

    f = (q1("SELECT max(fecha) AS f FROM fact_posicion WHERE cartera_id = ?", [cartera_id]) or {}).get("f")
    if f is None:
        raise HTTPException(404, "La cartera no tiene posiciones")
    posiciones = q("""
        SELECT p.especie, p.cantidad, p.precio, p.valor_cop,
               CASE WHEN p.especie = 'EFECTIVO' THEN 'EFECTIVO' ELSE coalesce(a.clase_activo_id, 'OTRO') END AS clase_activo_id,
               coalesce(e.nombre_corto, a.emisor_id) AS emisor, coalesce(a.nemotecnico, e.nombre_corto, p.especie) AS nombre
        FROM fact_posicion p LEFT JOIN dim_activo a USING (especie) LEFT JOIN dim_emisor e ON e.emisor_id = a.emisor_id
        WHERE p.cartera_id = ? AND p.fecha = ?""", [cartera_id, f])
    asignacion = q("""SELECT clase_activo_id, peso_objetivo FROM fact_asignacion
                      WHERE cartera_id = ? AND fecha = ? AND peso_objetivo IS NOT NULL""", [cartera_id, f])
    if not asignacion:
        raise HTTPException(409, "Esta cartera no tiene un perfil con asignación objetivo: asígnele uno en Carteras y transacciones")
    maximo = None
    if _graf_tabla_existe("dim_perfil", ("maximo_por_emisor",)) and _graf_tabla_existe("dim_cartera", ("perfil_id",)):
        maximo = (q1("""SELECT p.maximo_por_emisor AS m FROM dim_cartera c JOIN dim_perfil p ON p.perfil_id = c.perfil_id
                        WHERE c.cartera_id = ? ORDER BY p.version DESC LIMIT 1""", [cartera_id]) or {}).get("m")
    try:
        r = rebalanceo.calcular(posiciones, {a["clase_activo_id"]: a["peso_objetivo"] for a in asignacion},
                                aporte=aporte, modo=modo, comision=comision, maximo_por_emisor=maximo)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return responder({"fecha": f, **r})


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
    ahora = precios_en_curso()
    hora = None
    for p in posiciones:
        e = ahora.get(p["especie"])
        if e and p.get("cantidad") is not None:
            p["precio_en_curso"], p["variacion_en_curso"] = e["precio"], e["variacion"]
            p["valor_en_curso"] = p["cantidad"] * e["precio"]  # valor_cop = cantidad × precio (cartera.py)
            hora = max(hora or e["hora"], e["hora"])
    return responder({"fecha": f, "posiciones": posiciones, "asignacion": asignacion, "desempeno": desempeno,
                      "valor": valor, "dividendos": dividendos, "hora_en_curso": hora})


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
    filas = cuerpo.get("filas") or []
    if "especie" in usuario.TABLAS[tabla]["columnas"]:
        # Se puede escribir el nemotécnico (ECOPETROL) o el nombre corto en vez del código SFC de la especie
        filas = [{**f, "especie": resolver_especie(str(f["especie"]).strip())} if f.get("especie") not in (None, "") else f
                 for f in filas]
    try:
        nuevas = _tipar(tabla, filas)
    except (ValueError, TypeError) as e:
        return responder({"ok": False, "problemas": [f"dato inválido: {e}"]})
    if tabla == "dim_alerta" and nuevas.height:
        nuevas = nuevas.with_columns(pl.when(pl.col("alerta_id").is_null())
                                     .then(pl.Series([usuario.nuevo_id() for _ in range(nuevas.height)]))
                                     .otherwise(pl.col("alerta_id")).alias("alerta_id"))
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
    "leer_en_curso": ["ingest", "bvc_en_curso"],         # precio durante la sesión (15 min de retraso), aparte
    "leer_estados_ventana": ["estados-ventana"],        # lectura manual con ventana de los sitios protegidos
    "actualizar_modelo": ["modelo", "--con-dims"],  # dims primero: ahí entran los precios leídos de la Bolsa
    "rehacer_base": ["modelo", "--sin-calcular"],
    # Incorpora lo decidido en la revisión de estados sin leer documentos nuevos ni llamar a la API
    "incorporar_revision": ["ingest", "estados_compania"],
    "leer_paginas_indicadas": ["ingest", "estados_compania"],  # solo los documentos con páginas puestas a mano
}
# Acciones que corren la lectura de estados: no dos a la vez
_ACCIONES_ESTADOS = {"incorporar_revision", "leer_paginas_indicadas"}
_trabajos: dict[str, dict] = {}


def _correr(trabajo_id: str, argumentos: list[str]) -> None:
    t = _trabajos[trabajo_id]
    try:
        entorno = dict(os.environ)
        if t["accion"] in ("leer_bvc", "leer_en_curso", "leer_estados_ventana"):
            entorno["INVEST_LECTURA_MANUAL"] = "1"  # la pidió una persona desde el aplicativo
        if t["accion"] == "incorporar_revision":
            entorno["INVEST_EE_MAX_DOCUMENTOS"] = "0"
            entorno["INVEST_EE_MAX_ARBITRAJES"] = "0"
        if t["accion"] == "leer_paginas_indicadas":  # llama a la API: dos agentes por documento
            entorno["INVEST_EE_SOLO_INDICADAS"] = "1"
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
    if accion in _ACCIONES_ESTADOS and any(t["accion"] in _ACCIONES_ESTADOS and t["fin"] is None for t in _trabajos.values()):
        return responder({"ok": False, "problemas": ["ya hay una lectura de estados corriendo; espere a que termine"]})
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


# =========================================================================== Graficador
# Página #/graficador: varias acciones (y los índices) en una misma gráfica. Bloque propio para que
# integrar otras páginas no choque con este código.
GRAF_MAX_ACCIONES = 6
# Periodos en días hacia atrás desde la última fecha con dato de las series elegidas (None: toda la historia)
# Días hacia atrás desde el último dato; "ytd" (lo corrido del año) va desde el último cierre del año anterior
GRAF_PERIODOS: dict[str, int | str | None] = {"1s": 7, "1m": 31, "3m": 92, "6m": 182, "ytd": "ytd", "1a": 365,
                                              "3a": 1095, "5a": 1826, "10a": 3652, "max": None}
# Métricas permitidas (lista cerrada). unidad: indice (base 100), veces, porcentaje (fracción) o puntaje.
# variacion: relativa (final / inicial - 1) o puntos (final - inicial).
GRAF_METRICAS: dict[str, dict] = {
    "precio": {"nombre": "Precio (base 100)", "unidad": "indice", "base100": True, "variacion": "relativa",
               "frecuencia": "diaria", "fuente": "fact_precio_diario",
               "descripcion": "Precio de cada sesión, sin dividendos, igualado a 100 en la fecha base."},
    "retorno_total": {"nombre": "Retorno total (base 100)", "unidad": "indice", "base100": True,
                      "variacion": "relativa", "frecuencia": "diaria", "fuente": "fact_retorno_diario",
                      "descripcion": "Índice de retorno total: precio más dividendos reinvertidos, con splits y escisiones."},
    "retorno_usd": {"nombre": "Retorno total en dólares (base 100)", "unidad": "indice", "base100": True,
                    "variacion": "relativa", "frecuencia": "diaria", "fuente": "fact_retorno_diario",
                    "descripcion": "Índice de retorno total dividido por la TRM de cada día."},
    "p_vl": {"nombre": "Precio / valor en libros", "unidad": "veces", "base100": False, "variacion": "relativa",
             "frecuencia": "mensual", "fuente": "fact_valoracion", "variable": "p_vl",
             "descripcion": "Cuántas veces paga el mercado el patrimonio por acción conocido a la fecha."},
    "p_u": {"nombre": "Precio / utilidad", "unidad": "veces", "base100": False, "variacion": "relativa",
            "frecuencia": "mensual", "fuente": "fact_valoracion", "variable": "p_u",
            "descripcion": "Precio sobre la utilidad por acción de 12 meses; vacío si la utilidad es negativa."},
    "rendimiento_dividendo": {"nombre": "Rendimiento del dividendo 12 meses", "unidad": "porcentaje",
                              "base100": False, "variacion": "puntos", "frecuencia": "mensual",
                              "fuente": "fact_valoracion", "variable": "rendimiento_dividendo_12m",
                              "descripcion": "Dividendos de los últimos 12 meses sobre el precio, a cada fin de mes."},
    "volatilidad_60": {"nombre": "Volatilidad 60 días (anual)", "unidad": "porcentaje", "base100": False,
                       "variacion": "puntos", "frecuencia": "diaria", "fuente": "fact_tecnico_diario",
                       "descripcion": "Desviación de los retornos diarios de las últimas 60 sesiones, anualizada."},
    "puntaje_global": {"nombre": "Puntaje global (1 a 10)", "unidad": "puntaje", "base100": False,
                       "variacion": "puntos", "frecuencia": "mensual", "fuente": "fact_matriz",
                       "variable": "puntaje_global",
                       "descripcion": "Puntaje global de la matriz a cada fin de mes (informativo, no es una recomendación)."},
}
# Índices que se pueden sumar a la gráfica (solo en las métricas en base 100)
GRAF_INDICES: dict[str, dict] = {
    "universo_igual": {"nombre": "Universo (pesos iguales)", "tabla": "fact_indice_mercado",
                       "nota": "índice de retorno total del universo líquido (con dividendos)"},
    "universo_cap": {"nombre": "Universo (por capitalización)", "tabla": "fact_indice_mercado",
                     "nota": "índice de retorno total del universo líquido (con dividendos)"},
    "colcap": {"nombre": "MSCI COLCAP", "tabla": "fact_macro", "serie_id": "msci_colcap",
               "nota": "índice de precios, sin dividendos"},
}


def _sector(alias: str = "e") -> str:
    """Expresión SQL del sector por negocio del emisor (config/sectores.yaml), o el de la SFC si la base
    aún no tiene la columna (antes de correr invest dims)."""
    if _graf_tabla_existe("dim_emisor", ("sector",)):
        return f"coalesce({alias}.sector, {alias}.sector_sfc)"
    return f"{alias}.sector_sfc"


def _graf_tabla_existe(tabla: str, columnas: tuple[str, ...] = ()) -> bool:
    filas = q("SELECT column_name FROM information_schema.columns WHERE table_name = ?", [tabla])
    nombres = {f["column_name"] for f in filas}
    return bool(nombres) and all(c in nombres for c in columnas)


def _graf_fuente_valoracion() -> str:
    """``fact_valoracion_diaria`` (especie, fecha, p_vl, p_u) si ya existe en la base; si no, la mensual."""
    if _graf_tabla_existe("fact_valoracion_diaria", ("especie", "fecha", "p_vl", "p_u")):
        return "fact_valoracion_diaria"
    return "fact_valoracion"


def _graf_metrica(metrica: str) -> dict:
    if metrica not in GRAF_METRICAS:
        raise HTTPException(400, f"Métrica no permitida: {metrica}. Use una de: {', '.join(GRAF_METRICAS)}")
    m = {"id": metrica, **GRAF_METRICAS[metrica]}
    if metrica in ("p_vl", "p_u") and _graf_fuente_valoracion() == "fact_valoracion_diaria":
        m.update(fuente="fact_valoracion_diaria", frecuencia="diaria")
    return m


def _graf_sql_accion(metrica: str, fuente: str) -> str:
    """Consulta (fecha, valor) de una especie para la métrica; el único parámetro es la especie."""
    if metrica == "precio":
        return "SELECT fecha, precio AS valor FROM fact_precio_diario WHERE especie = ?"
    if metrica == "retorno_total":
        return "SELECT fecha, indice_rt AS valor FROM fact_retorno_diario WHERE especie = ?"
    if metrica == "retorno_usd":
        return """SELECT r.fecha, r.indice_rt / p.trm AS valor FROM fact_retorno_diario r
                  JOIN fact_precio_diario p ON p.especie = r.especie AND p.fecha = r.fecha
                  WHERE r.especie = ? AND p.trm > 0"""
    if metrica in ("p_vl", "p_u"):
        tabla = "fact_valoracion_diaria" if fuente == "fact_valoracion_diaria" else "fact_valoracion"
        return f"SELECT fecha, {metrica} AS valor FROM {tabla} WHERE especie = ?"
    if metrica == "rendimiento_dividendo":
        return "SELECT fecha, rendimiento_dividendo_12m AS valor FROM fact_valoracion WHERE especie = ?"
    if metrica == "volatilidad_60":
        return "SELECT fecha, volatilidad_60 AS valor FROM fact_tecnico_diario WHERE especie = ?"
    if metrica == "puntaje_global":
        return "SELECT fecha, puntaje_global AS valor FROM fact_matriz WHERE especie = ?"
    raise HTTPException(400, f"Métrica no permitida: {metrica}")


def _graf_filas_indice(indice: str, metrica: str) -> list[dict]:
    usd = metrica == "retorno_usd"
    if indice in ("universo_igual", "universo_cap"):
        return q("SELECT fecha, valor FROM fact_indice_mercado WHERE indice_id = ? AND moneda = ?",
                 [indice, "USD" if usd else "COP"])
    serie = GRAF_INDICES[indice]["serie_id"]
    if usd:
        return q("""SELECT m.fecha, m.valor / t.valor AS valor FROM fact_macro m
                    JOIN fact_macro t ON t.fecha = m.fecha AND t.serie_id = 'trm_usd_cop'
                    WHERE m.serie_id = ? AND t.valor > 0""", [serie])
    return q("SELECT fecha, valor FROM fact_macro WHERE serie_id = ?", [serie])


def _graf_numero(v: Any) -> float | None:
    """El valor como float finito (también DECIMAL de DuckDB), o None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def graf_resolver(tokens: list[str]) -> tuple[list[dict], list[str]]:
    """Series pedidas → ([{id, tipo, especie, nemotecnico, nombre, sector}], desconocidas).

    Acepta índices (``universo_igual``, ``universo_cap``, ``colcap``), código SFC, nemotécnico o nombre
    corto (``resolver_especie``). Las repetidas cuentan una vez.
    """
    series, desconocidas, vistos = [], [], set()
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if t.lower() in GRAF_INDICES:
            i = t.lower()
            if i not in vistos:
                vistos.add(i)
                series.append({"id": i, "tipo": "indice", "especie": None, "nemotecnico": None,
                               "nombre": GRAF_INDICES[i]["nombre"], "sector": None})
            continue
        especie = resolver_especie(t)
        fila = q1(f"""SELECT a.especie, a.nemotecnico, e.nombre_corto, a.nombre, {_sector()} AS sector_sfc
                     FROM dim_activo a LEFT JOIN dim_emisor e USING (emisor_id) WHERE a.especie = ?""", [especie])
        if fila is None:
            desconocidas.append(t)
            continue
        if especie in vistos:
            continue
        vistos.add(especie)
        series.append({"id": especie, "tipo": "accion", "especie": especie, "nemotecnico": fila["nemotecnico"],
                       "nombre": fila["nombre_corto"] or fila["nemotecnico"] or fila["nombre"] or especie,
                       "sector": fila["sector_sfc"]})
    # Dos especies del mismo emisor (ordinaria y preferencial) se distinguen por el nemotécnico
    repetidos = {s["nombre"] for s in series if sum(o["nombre"] == s["nombre"] for o in series) > 1}
    for s in series:
        if s["tipo"] == "accion" and s["nombre"] in repetidos:
            s["nombre"] = f"{s['nombre']} ({s['nemotecnico'] or s['especie']})"
    return series, desconocidas


def graf_resumen(fechas: list, valores: list, variacion: str) -> dict:
    """Valor inicial y final del periodo, variación, máximo y mínimo (con su fecha) de una serie alineada."""
    puntos = [(f, v) for f, v in zip(fechas, valores) if v is not None]
    if not puntos:
        return {"observaciones": 0}
    (fi, vi), (ff, vf) = puntos[0], puntos[-1]
    fmax, vmax = max(puntos, key=lambda p: p[1])
    fmin, vmin = min(puntos, key=lambda p: p[1])
    if variacion == "relativa":
        cambio = vf / vi - 1 if vi else None
    else:
        cambio = vf - vi
    return {"observaciones": len(puntos), "inicial": vi, "fecha_inicial": fi, "final": vf, "fecha_final": ff,
            "variacion": cambio, "maximo": vmax, "fecha_maximo": fmax, "minimo": vmin, "fecha_minimo": fmin}


def _graf_base100(datos: dict[str, dict], fechas: list) -> tuple[date | None, bool]:
    """Divide cada serie por su valor en el primer día común (en el lugar). (fecha_base, hubo_comun)."""
    con_datos = [d for d in datos.values() if d]
    if not con_datos:
        return None, False
    comunes = set.intersection(*[set(d) for d in con_datos])
    fecha_base = min(comunes) if comunes else None
    for d in con_datos:
        base = d[fecha_base] if fecha_base else d[min(d)]
        for f in d:
            d[f] = d[f] / base * 100
    return fecha_base, fecha_base is not None


def graficar(tokens: list[str], metrica: str, periodo: str) -> dict:
    """Series alineadas por fecha (unión de fechas, sin rellenar huecos) y metadatos de la métrica.

    En base 100 cada serie se divide por su valor en la primera fecha del periodo en que todas tienen
    dato (``fecha_base``); si no coinciden en ninguna, cada una se normaliza a su primer dato.
    """
    from datetime import timedelta

    m = _graf_metrica(metrica)
    if periodo not in GRAF_PERIODOS:
        raise HTTPException(400, f"Periodo no permitido: {periodo}. Use uno de: {', '.join(GRAF_PERIODOS)}")
    tokens = [t.strip() for t in tokens if t.strip()]
    if not tokens:
        raise HTTPException(400, "Indique al menos una serie (parámetro series)")
    pedidas = {t.lower() for t in tokens if t.lower() not in GRAF_INDICES}
    if len(pedidas) > GRAF_MAX_ACCIONES:  # antes de consultar nada
        raise HTTPException(400, f"Máximo {GRAF_MAX_ACCIONES} acciones por gráfica (llegaron {len(pedidas)})")
    series, desconocidas = graf_resolver(tokens)
    if desconocidas:
        raise HTTPException(404, f"Serie desconocida: {', '.join(desconocidas)}")
    acciones = [s for s in series if s["tipo"] == "accion"]
    if len(acciones) > GRAF_MAX_ACCIONES:
        raise HTTPException(400, f"Máximo {GRAF_MAX_ACCIONES} acciones por gráfica (llegaron {len(acciones)})")
    avisos: list[str] = []
    if not m["base100"]:
        fuera = [s for s in series if s["tipo"] == "indice"]
        if fuera:
            avisos.append("Los índices solo se grafican en base 100; " + ", ".join(s["nombre"] for s in fuera)
                          + " no aplica en " + m["nombre"].lower() + ".")
        series = acciones
    positiva = m["base100"] or m["unidad"] == "veces"  # precios, índices y múltiplos solo tienen sentido > 0
    datos: dict[str, dict] = {}
    for s in series:
        filas = _graf_filas_indice(s["id"], metrica) if s["tipo"] == "indice" else \
            q(_graf_sql_accion(metrica, m["fuente"]), [s["especie"]])
        leidos = ((f["fecha"], _graf_numero(f["valor"])) for f in filas)
        datos[s["id"]] = {f: x for f, x in leidos if x is not None and (not positiva or x > 0)}
    ultimas = [max(d) for d in datos.values() if d]
    hasta = max(ultimas) if ultimas else None
    dias = GRAF_PERIODOS[periodo]
    if dias == "ytd" and hasta is not None:
        # Base: el último dato del año anterior (de cualquier serie), para que la variación sea la del año
        previos = [f for d in datos.values() for f in d if f < date(hasta.year, 1, 1)]
        desde = max(previos) if previos else date(hasta.year, 1, 1)
    else:
        desde = hasta - timedelta(days=dias) if hasta is not None and dias is not None else None
    if desde is not None:
        datos = {k: {f: v for f, v in d.items() if f >= desde} for k, d in datos.items()}
    fechas = sorted(set().union(*[set(d) for d in datos.values()]))
    fecha_base = None
    if m["base100"] and fechas:
        fecha_base, comun = _graf_base100(datos, fechas)
        if not comun:
            avisos.append("Las series no coinciden en ninguna fecha: cada una se normaliza a 100 en su primer dato.")
        elif fecha_base > fechas[0]:
            tardias = [s for s in series if datos[s["id"]] and min(datos[s["id"]]) == fecha_base]
            avisos.append(f"Base 100 el {fecha_base.isoformat()}: primer día con dato de todas las series"
                          + (" (primer dato en el periodo de " + ", ".join(s["nombre"] for s in tardias) + ")" if tardias else "")
                          + ". Antes de esa fecha cada línea muestra su valor frente a esa base.")
    for s in series:
        if not datos[s["id"]]:
            avisos.append(f"{s['nombre']} no tiene datos de {m['nombre'].lower()} en el periodo.")
        elif s["tipo"] == "indice" and (metrica == "precio" or s["id"] == "colcap"):
            avisos.append(f"{s['nombre']}: {GRAF_INDICES[s['id']]['nota']}.")
    valores = {s["id"]: [round(datos[s["id"]][f], 6) if f in datos[s["id"]] else None for f in fechas] for s in series}
    resumen = [{"id": s["id"], **graf_resumen(fechas, valores[s["id"]], m["variacion"])} for s in series]
    for s in series:
        d = datos[s["id"]]
        s.update(observaciones=len(d), primera_fecha=min(d) if d else None, ultima_fecha=max(d) if d else None)
    return {"metrica": m, "periodo": {"id": periodo, "desde": desde or (fechas[0] if fechas else None), "hasta": hasta},
            "fecha_base": fecha_base, "series": series, "fechas": fechas, "valores": valores, "resumen": resumen,
            "avisos": avisos}


@app.get("/api/graficador")
def graficador(series: str = "", metrica: str = "retorno_total", periodo: str = "1a"):
    return responder(graficar(series.split(","), metrica, periodo))


@app.get("/api/graficador/opciones")
def graficador_opciones():
    """Métricas, periodos e índices con datos (el COLCAP solo si ya tiene historia en fact_macro)."""
    indices = []
    for i, d in GRAF_INDICES.items():
        if d["tabla"] == "fact_indice_mercado":
            r = q1("""SELECT min(fecha) AS desde, max(fecha) AS hasta, count(*) AS n FROM fact_indice_mercado
                      WHERE indice_id = ? AND moneda = 'COP'""", [i])
        else:
            r = q1("SELECT min(fecha) AS desde, max(fecha) AS hasta, count(*) AS n FROM fact_macro WHERE serie_id = ?",
                   [d["serie_id"]])
        if r and r["n"]:
            indices.append({"id": i, "nombre": d["nombre"], "nota": d["nota"], "desde": r["desde"],
                            "hasta": r["hasta"], "observaciones": r["n"]})
    metricas = [_graf_metrica(k) for k in GRAF_METRICAS]
    return responder({"metricas": metricas, "periodos": list(GRAF_PERIODOS), "indices": indices,
                      "max_acciones": GRAF_MAX_ACCIONES})
# =========================================================================== fin Graficador


# --------------------------------------------------------------------------- páginas
@app.get("/")
def inicio():
    return FileResponse(ESTATICOS / "index.html")


@app.get("/tablero")
def tablero_validado():
    """El tablero de la matriz de factores validados (invest ficha)."""
    archivo = data_dir() / "tableros" / "matriz_factores.html"
    if not archivo.exists():
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>Tablero de factores</title>"
            "<body style='font-family:system-ui;background:#0B1220;color:#E6EAF2;padding:40px'>"
            "<h1 style='font-size:20px'>El tablero de los factores validados aún no existe</h1>"
            "<p>Se genera con <code>uv run invest ficha</code> (la tarea diaria del PC lo corre después de "
            "<code>invest factores</code>). Luego vuelva a abrir este enlace.</p><p><a style='color:#6E9BFF' href='/#/matriz'>"
            "Volver a la matriz</a></p></body>", status_code=404)
    # Abierto desde el aplicativo, el tablero usa su tema oscuro (el archivo solo sigue al del sistema)
    html = archivo.read_text(encoding="utf-8").replace('<html lang="es">', '<html lang="es" data-theme="dark">', 1)
    return HTMLResponse(html)


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

