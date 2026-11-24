"""Lectura manual, con ventana, de los estados de las compañías cuyo sitio bloquea a los programas.

Cementos Argos, Celsia, Nutresa y Grupo Argos protegen sus sitios contra robots (Imperva Incapsula u
otro desafío) y el conector automático (``estados_compania``) no lo esquiva. Esta lectura es la vía "en
vivo", igual que la de la Bolsa de Colombia (decisión de Esteban, 24-nov-2026):

- La lanza Esteban (consola o botón del aplicativo), nunca una tarea programada (``bvc.hay_consola``).
- Abre una ventana normal de Chromium en su PC. Si el sitio muestra una verificación o un captcha, la
  resuelve él en la ventana; el programa no la toca, solo espera a que la página real aparezca.
- Con la página ya abierta, guarda en ``invest-data/fuentes_compania/<emisor_id>/`` los PDF de estados
  consolidados enlazados ahí (los patrones de ``config/estados_compania.yaml``) y cualquier PDF que él
  abra en la ventana mientras está abierta. Los descarga con la misma sesión de la ventana, con pausas.
- Después, ``invest ingest estados_compania`` los procesa como los demás (carpeta manual).

Cada emisor con ``ventana: true`` en el YAML entra; ``paginas_ventana`` (o ``paginas_indice``) dice qué
páginas abrir. Si no hay página conocida, se abre ``inicio`` y Esteban navega hasta los informes.
"""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

from invest.config import data_dir
from invest.ingest import estados_compania as ec

PATRON_GENERICO = r"(consolidad|consolidated).*\.pdf|(estados?[-_ ]financier|financial[-_ ]statements|eeff|reporte[-_ ]?[1-4]t).*\.pdf"


class SinPersona(RuntimeError):
    """La lectura con ventana solo la lanza una persona."""


def destino(emisor_id: str) -> Path:
    carpeta = data_dir() / ec.CARPETA_MANUAL / emisor_id
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def nombre_destino(url: str, texto: str = "") -> str:
    """``<corte>_<archivo>.pdf`` (el corte ayuda a la carpeta manual a ubicar el informe)."""
    nombre = ec._nombre_archivo(url)
    corte = ec.corte_probable(url, texto)
    return f"{corte.isoformat()}_{nombre}" if corte and not nombre.startswith(corte.isoformat()) else nombre


def candidatos_ventana(emisor: dict, enlaces: list[tuple[str, str]]) -> list[dict]:
    """Los del patrón del emisor; si no tiene, uno genérico de estados financieros consolidados."""
    e = dict(emisor)
    if not e.get("patrones"):
        e["patrones"] = [PATRON_GENERICO]
        e.setdefault("excluir", ["separad", "separate", "presentaci", "comunicado", "press"])
    return ec.candidatos(e, enlaces)


def guardar(emisor_id: str, url: str, contenido: bytes, texto: str = "") -> Path | None:
    """Guarda el PDF si es un PDF de verdad y no estaba ya (mismo nombre)."""
    if not ec.es_pdf(contenido):
        return None
    ruta = destino(emisor_id) / nombre_destino(url, texto)
    if ruta.exists():
        return None
    ruta.write_bytes(contenido)
    return ruta


def _esperar_pagina_real(pagina, espera_s: int, avisar) -> bool:
    """Espera a que desaparezca la verificación del sitio (la resuelve la persona en la ventana)."""
    inicio, avisado = time.monotonic(), False
    while time.monotonic() - inicio < espera_s:
        try:
            html = pagina.content().encode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 - la página está navegando
            html = b""
        if html and not ec.es_bloqueo(html):
            return True
        if not avisado:
            avisar("  el sitio pide una verificación: resuélvala en la ventana; el programa espera")
            avisado = True
        pagina.wait_for_timeout(2000)
    return False


def leer(emisores: list[str] | None = None, espera_s: int = 300, abierta_s: int = 120, maximo: int = 8,
         navegador_factory=None, avisar=print, hoy: date | None = None) -> dict:
    """Abre la ventana y guarda los PDF. Devuelve el conteo por emisor.

    ``abierta_s``: tiempo que la ventana queda abierta por emisor después de leer la página, para que
    Esteban abra otros informes si quiere (cada PDF que abra se guarda). Se puede cerrar antes.
    """
    from invest.ingest.bvc import hay_consola

    if not hay_consola():
        raise SinPersona("la lectura con ventana la lanza una persona (consola o botón del aplicativo), "
                         "nunca una tarea programada")
    cfg = ec.config()
    lista = [e for e in cfg.get("emisores", []) if e.get("ventana")]
    if emisores:
        lista = [e for e in lista if e["emisor_id"] in emisores]
    resumen: dict[str, dict] = {}
    if navegador_factory is None:
        from playwright.sync_api import sync_playwright

        def navegador_factory():
            return sync_playwright()
    with navegador_factory() as p:
        navegador = p.chromium.launch(headless=False)
        try:
            contexto = navegador.new_context(locale="es-CO", accept_downloads=True)
            for emisor in lista:
                eid = emisor["emisor_id"]
                r = resumen.setdefault(eid, {"guardados": 0, "ya_estaban": 0, "sin_pagina_real": 0, "abiertos": 0})
                abiertos: list = []

                def al_responder(resp, eid=eid, abiertos=abiertos):
                    tipo = (resp.headers or {}).get("content-type", "")
                    if "pdf" in tipo.lower() or resp.url.lower().split("?")[0].endswith(".pdf"):
                        abiertos.append(resp)

                contexto.on("response", al_responder)
                paginas = [x["url"] for x in (emisor.get("paginas_ventana") or emisor.get("paginas_indice") or [])]
                if not paginas and emisor.get("inicio"):
                    paginas = [emisor["inicio"]]
                avisar(f"{emisor.get('nombre', eid)}: {len(paginas)} página(s)")
                pagina = contexto.new_page()
                for url in paginas:
                    pagina.goto(url, wait_until="domcontentloaded", timeout=90_000)
                    if not _esperar_pagina_real(pagina, espera_s, avisar):
                        r["sin_pagina_real"] += 1
                        continue
                    pagina.wait_for_timeout(3000)  # deja terminar de armar los enlaces
                    enlaces = ec.enlaces(pagina.content(), pagina.url)
                    for cand in candidatos_ventana(emisor, enlaces)[:maximo]:
                        if (destino(eid) / nombre_destino(cand["url"], cand.get("texto", ""))).exists():
                            r["ya_estaban"] += 1
                            continue
                        resp = contexto.request.get(cand["url"], timeout=120_000)
                        cuerpo = resp.body() if resp.ok else b""
                        if ec.es_bloqueo(cuerpo):
                            # El PDF pide su propia verificación: se abre en la ventana para que la resuelva
                            pagina.goto(cand["url"], wait_until="domcontentloaded", timeout=90_000)
                            _esperar_pagina_real(pagina, espera_s, avisar)
                            resp = contexto.request.get(cand["url"], timeout=120_000)
                            cuerpo = resp.body() if resp.ok else b""
                        if guardar(eid, cand["url"], cuerpo, cand.get("texto", "")):
                            r["guardados"] += 1
                        pagina.wait_for_timeout(2500)  # pausa entre descargas
                if abierta_s:
                    avisar(f"  la ventana queda abierta {abierta_s} s: los PDF que abra se guardan")
                    try:
                        pagina.wait_for_event("close", timeout=abierta_s * 1000)
                    except Exception:  # noqa: BLE001 - se acabó el tiempo
                        pass
                for resp in abiertos:
                    try:
                        if guardar(eid, resp.url, resp.body()):
                            r["abiertos"] += 1
                    except Exception:  # noqa: BLE001 - respuesta ya liberada
                        pass
                contexto.remove_listener("response", al_responder)
                try:
                    pagina.close()
                except Exception:  # noqa: BLE001
                    pass
                avisar(f"  guardados {r['guardados']}, abiertos por usted {r['abiertos']}, ya estaban {r['ya_estaban']}"
                       + (f", páginas sin pasar la verificación {r['sin_pagina_real']}" if r["sin_pagina_real"] else ""))
        finally:
            navegador.close()
    return resumen
