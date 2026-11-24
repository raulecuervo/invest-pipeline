"""Conector del Banco de la República: tasa de política monetaria e IBR (SUAMECA).

El portal de estadísticas SUAMECA no tiene descarga directa estable y no responde a lectura
automática desde centros de datos. Como con la Bolsa de Colombia, un Chromium sin ventana (en el
PC) abre la página de cada serie y el conector toma los datos que el propio portal descarga para
dibujar el gráfico (respuestas ``consultaMenuXId`` y ``consultaInformacionSerie``). No se llama a ese servicio por
fuera de la página ni se sortea ningún bloqueo.

Cada página trae la serie completa (la tasa de política desde 1998 y el IBR desde 2008) y, en la
del IBR, todos sus plazos (overnight, 1, 3, 6 y 12 meses, nominal y efectiva). Van a
silver/fact_serie_macro con serie_id ``banrep_<id>`` y el nombre del Banco en la bitácora.

Requiere el extra ``pc`` (Playwright) como el conector de la Bolsa de Colombia.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl

from invest import storage
from invest.ingest.base import Conector
from invest.ingest.dane_ipc import ESQUEMA

BOGOTA = ZoneInfo("America/Bogota")


def series_en(respuesta) -> list[dict]:
    """Series con datos dentro de una respuesta del portal.

    Una sola serie llega en ``consultaMenuXId`` como ``{"SERIES": [...]}``; varias (p. ej. todos los
    plazos del IBR) llegan en ``consultaInformacionSerie`` como lista.
    """
    lista = respuesta.get("SERIES") if isinstance(respuesta, dict) else respuesta
    return [s for s in (lista or []) if isinstance(s, dict) and s.get("data")]


def leer_series(url: str, timeout_s: int = 90) -> list[dict]:
    """Abre la página de la serie y devuelve los datos que la página recibe para su gráfico."""
    from playwright.sync_api import sync_playwright

    recibidas: list = []

    def al_responder(r):
        if r.status == 200 and ("consultaInformacionSerie" in r.url or "consultaMenuXId" in r.url):
            try:
                recibidas.append(r.json())
            except Exception:
                pass

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        try:
            pagina = navegador.new_page(locale="es-CO")
            pagina.on("response", al_responder)
            pagina.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            for _ in range(timeout_s * 2):  # espera hasta que llegue una respuesta con datos
                series = [s for r in recibidas for s in series_en(r)]
                if series:
                    # La página del IBR pide primero una serie y luego todos los plazos: se espera
                    # hasta que pasen 3 segundos sin respuestas nuevas (máximo 20)
                    vistas, quietas = len(recibidas), 0
                    for _ in range(40):
                        pagina.wait_for_timeout(500)
                        quietas = quietas + 1 if len(recibidas) == vistas else 0
                        vistas = len(recibidas)
                        if quietas >= 6:
                            break
                    return [s for r in recibidas for s in series_en(r)]
                pagina.wait_for_timeout(500)
            raise TimeoutError("La página del Banco de la República no entregó datos")
        finally:
            navegador.close()


def fecha_bogota(epoch_ms: int) -> date:
    """Los puntos vienen en milisegundos UTC a la medianoche de Bogotá (05:00 UTC)."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone(BOGOTA).date()


def aplanar(series: list[dict]) -> list[dict]:
    registros = []
    for s in series:
        for punto in s.get("data") or []:
            if len(punto) < 2 or punto[1] is None:
                continue
            registros.append({
                "fecha": fecha_bogota(int(punto[0])), "serie_id": f"banrep_{s['id']}", "valor": float(punto[1]),
                "unidad": s.get("unidadCorta") or s.get("unidad"),
                "frecuencia": (s.get("descripcionPeriodicidad") or "").lower() or None,
                "nombre": s.get("nombre"),
            })
    return registros


class ConectorBanRep(Conector):
    fuente_id = "banrep_tasas"
    # Una carpeta por fuente dentro de la tabla: así GitHub Actions y el PC nunca escriben el mismo
    # archivo (leer_tabla("silver", "fact_serie_macro") las une)
    tabla = "fact_serie_macro/fuente=banrep_tasas"
    claves = ("fecha", "serie_id")

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        registros = []
        for pagina in self.meta["paginas"]:
            series = leer_series(pagina["url"])
            self.crudos[f"pagina_{pagina['id']}.json"] = json.dumps(series, ensure_ascii=False).encode("utf-8")
            registros += aplanar(series)
        nombres = sorted({f"{r['serie_id']} = {r['nombre']}" for r in registros})
        if nombres:
            self.notas.append("; ".join(nombres))
        # La página trae la serie completa: si lo guardado empieza después de lo que la página ofrece
        # (p. ej. la primera carga quedó recortada), se guarda todo una vez y la serie queda completa
        guardado = storage.leer_tabla("silver", self.tabla)
        primero_web = min((r["fecha"] for r in registros), default=None)
        if primero_web and (guardado is None or guardado.is_empty()
                            or guardado.get_column("fecha").min() > primero_web + timedelta(days=30)):
            self.notas.append(f"serie completa desde {primero_web}")
            return registros
        return [r for r in registros if r["fecha"] >= desde]

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA)
        ahora = datetime.now(timezone.utc)
        return pl.DataFrame([{
            "fecha": r["fecha"], "serie_id": r["serie_id"], "valor": r["valor"], "unidad": r["unidad"],
            "frecuencia": r["frecuencia"], "fecha_publicacion": None, "publicacion_estimada": None,
            "fuente_id": self.fuente_id, "ingest_ts": ahora,
        } for r in registros], schema=ESQUEMA).unique(subset=["fecha", "serie_id"], keep="last")

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        motivo = pl.when((pl.col("valor") < -5) | (pl.col("valor") > 100)).then(pl.lit("tasa fuera de rango"))
        df = df.with_columns(motivo.otherwise(pl.lit(None)).alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
