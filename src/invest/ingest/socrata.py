"""Cliente mínimo para la API Socrata (SODA) de datos.gov.co.

- Pagina con $limit/$offset hasta traer todos los registros.
- Usa un app token gratuito si existe la variable SOCRATA_APP_TOKEN (sube el límite de consultas).
- Reintenta con espera creciente ante errores de red o respuestas 429/5xx.
- Se identifica con un User-Agent propio, como pide la buena práctica de extracción.
"""

from __future__ import annotations

import os
import time

import requests

USER_AGENT = os.environ.get(
    "INVEST_USER_AGENT", "invest-pipeline/0.1 (uso personal; contacto en el repositorio)"
)


class ErrorSocrata(RuntimeError):
    pass


def consultar(
    url: str,
    where: str | None = None,
    order: str | None = None,
    pagina: int = 50_000,
    reintentos: int = 4,
    timeout: int = 60,
    sesion: requests.Session | None = None,
) -> list[dict]:
    sesion = sesion or requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token

    registros: list[dict] = []
    offset = 0
    while True:
        params = {"$limit": pagina, "$offset": offset}
        if where:
            params["$where"] = where
        if order:
            params["$order"] = order
        lote = _pedir(sesion, url, params, headers, reintentos, timeout)
        registros.extend(lote)
        if len(lote) < pagina:
            return registros
        offset += pagina
        time.sleep(1)  # pausa entre páginas para no cargar el servidor


def _pedir(sesion, url, params, headers, reintentos, timeout) -> list[dict]:
    espera = 2
    for intento in range(1, reintentos + 1):
        try:
            r = sesion.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                datos = r.json()
                if not isinstance(datos, list):
                    raise ErrorSocrata(f"Respuesta inesperada de {url}: {str(datos)[:200]}")
                return datos
            if r.status_code in (429, 500, 502, 503, 504) and intento < reintentos:
                time.sleep(espera)
                espera *= 2
                continue
            raise ErrorSocrata(f"HTTP {r.status_code} en {url}: {r.text[:200]}")
        except requests.RequestException as e:
            if intento == reintentos:
                raise ErrorSocrata(f"Fallo de red consultando {url}: {e}") from e
            time.sleep(espera)
            espera *= 2
    raise ErrorSocrata(f"Sin respuesta de {url}")
