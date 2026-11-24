"""Descargas HTTP para fuentes sin API (páginas, archivos estáticos y formularios).

Reglas de extracción responsable (ver la sección de web scraping del documento maestro):
- Se identifica con un User-Agent propio (variable INVEST_USER_AGENT).
- Espera entre solicitudes (INVEST_PAUSA_SEGUNDOS, 1 segundo por defecto) para no cargar el sitio.
- Reintenta con espera creciente ante errores de red o respuestas 429/5xx.
- Un 404 no se reintenta: se informa con NoEncontrado para que el conector lo omita.
- No usa tokens de terceros, no resuelve captchas ni evita bloqueos. Si el sitio responde 401 o 403,
  se detiene y lo informa. Una sesión con cookie y el estado de un formulario (p. ej. el ViewState
  de JSF) son parte del uso normal de una página y sí se conservan.
"""

from __future__ import annotations

import os
import time

import requests

from invest.ingest.socrata import USER_AGENT


class ErrorHTTP(RuntimeError):
    pass


class NoEncontrado(ErrorHTTP):
    pass


class AccesoDenegado(ErrorHTTP):
    pass


_sesion: requests.Session | None = None
_ultima: float = 0.0


def _pausa() -> float:
    return float(os.environ.get("INVEST_PAUSA_SEGUNDOS", "1.0"))


def nueva_sesion() -> None:
    """Descarta las cookies de la sesión compartida (p. ej. antes de un formulario nuevo)."""
    global _sesion
    _sesion = None


def solicitar(
    metodo: str,
    url: str,
    datos: dict | None = None,
    cabeceras: dict | None = None,
    reintentos: int = 4,
    timeout: int = 60,
    permitir_304: bool = False,
) -> requests.Response:
    """GET o POST con pausa, reintentos y User-Agent propio. Devuelve la respuesta 200 (o 304)."""
    global _sesion, _ultima
    _sesion = _sesion or requests.Session()
    encabezados = {"User-Agent": USER_AGENT, **(cabeceras or {})}
    ultimo_error = ""
    for intento in range(reintentos):
        espera = _pausa() - (time.monotonic() - _ultima)
        if espera > 0:
            time.sleep(espera)
        try:
            r = _sesion.request(metodo, url, data=datos, headers=encabezados, timeout=timeout)
        except requests.RequestException as e:
            ultimo_error = f"{type(e).__name__}"
        else:
            _ultima = time.monotonic()
            if r.status_code == 200 or (r.status_code == 304 and permitir_304):
                return r
            if r.status_code == 404:
                raise NoEncontrado(f"404 en {url}")
            if r.status_code in (401, 403):
                raise AccesoDenegado(f"{r.status_code} en {url}: el sitio no permite el acceso")
            ultimo_error = f"HTTP {r.status_code}"
            if r.status_code != 429 and r.status_code < 500:
                break
        _ultima = time.monotonic()
        time.sleep(min(2 ** intento * 2, 30))
    raise ErrorHTTP(f"No se pudo {'enviar a' if metodo == 'POST' else 'descargar'} {url}: {ultimo_error}")


def obtener(url: str, reintentos: int = 4, timeout: int = 60) -> bytes:
    """Descarga ``url`` y devuelve los bytes de la respuesta."""
    return solicitar("GET", url, reintentos=reintentos, timeout=timeout).content


def obtener_condicional(
    url: str, previo: dict | None = None, reintentos: int = 4, timeout: int = 60,
    cabeceras: dict | None = None,
) -> tuple[bytes | None, dict]:
    """Descarga ``url`` solo si cambió desde la última vez.

    ``previo`` trae los validadores que el servidor dio en la descarga anterior (ETag y
    Last-Modified). Si el archivo no cambió, el servidor responde 304 sin contenido y esta función
    devuelve (None, previo). Si cambió o no hay validadores, devuelve (bytes, validadores nuevos).
    """
    cabeceras = dict(cabeceras or {})
    if previo:
        if previo.get("etag"):
            cabeceras["If-None-Match"] = previo["etag"]
        if previo.get("last_modified"):
            cabeceras["If-Modified-Since"] = previo["last_modified"]
    r = solicitar("GET", url, cabeceras=cabeceras, reintentos=reintentos, timeout=timeout, permitir_304=True)
    if r.status_code == 304:
        return None, dict(previo or {})
    validadores = {
        k: v for k, v in (("etag", r.headers.get("ETag")), ("last_modified", r.headers.get("Last-Modified"))) if v
    }
    return r.content, validadores
