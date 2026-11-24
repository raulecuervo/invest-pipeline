"""Prueba de humo de las páginas del aplicativo local con un navegador sin ventana.

Arranca el servidor (``invest.app.servidor``) en un puerto libre con la carpeta de datos indicada,
abre cada página con Chromium headless, espera a que termine de cargar y la marca como fallida si:

- hubo errores en la consola del navegador o excepciones de JavaScript sin atrapar;
- algún pedido a ``/api/*`` respondió con código >= 400 (salvo los intencionales de ``PERMITIDOS``),
  con una redirección, con ``undefined``/``null``/``NaN`` en la ruta, o no llegó a responder;
- la página quedó vacía, en "Cargando…", con el panel de error de la app o con texto como
  ``undefined`` / ``NaN`` (un dato que el código no esperaba).

Uso (desde la raíz del repositorio)::

    uv run --extra app --extra pc python scripts/probar_paginas.py --datos ../invest-data
    python scripts/probar_paginas.py --datos /tmp/demo --python .venv/bin/python --capturas /tmp/capturas

El servidor corre con ``--python`` (por omisión la ``.venv`` del repositorio si existe, o el mismo
intérprete); el navegador, con el intérprete que ejecuta este script, que necesita Playwright y
Chromium (``playwright install chromium``). No modifica los datos: las páginas solo leen. Si la carpeta
no tiene ``modelo.duckdb``, el servidor la construye al arrancar (como ``invest app``).

Código de salida: 0 si todas las páginas pasan, 1 si alguna falla, 2 si el servidor no arrancó.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# Páginas fijas del enrutador (static/app.js); las que llevan parámetro se agregan con datos reales
PAGINAS_FIJAS = ["mercado", "screener", "matriz", "ficha", "cartera", "carteras", "perfiles", "tesis", "salud",
                 "aprender", "valoracion", "graficador"]

# Respuestas >= 400 de /api/* que son parte del comportamiento esperado: (expresión sobre la ruta, código)
PERMITIDOS: list[tuple[str, int]] = []

# Página que debe mostrar un error controlado: una especie que no existe responde 404 y la ficha lo dice
PAGINA_NEGATIVA = "ficha/__no_existe__"
PERMITIDOS_NEGATIVA = [(r"^/api/ficha/__no_existe__$", 404)]

SOSPECHOSOS = re.compile(r"\b(undefined|NaN|null|\[object Object\]|Infinity)\b")
# Un parámetro que el JavaScript no tenía: /api/macro/undefined, ?dias=NaN
URL_SOSPECHOSA = re.compile(r"(/|=)(undefined|null|NaN)(/|&|\?|$)")


@dataclass
class Resultado:
    ruta: str
    ok: bool = True
    segundos: float = 0.0
    pedidos_api: int = 0
    titulo: str = ""
    problemas: list[str] = field(default_factory=list)

    def fallar(self, motivo: str) -> None:
        self.ok = False
        self.problemas.append(motivo)


# --------------------------------------------------------------------------- servidor
def puerto_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def python_servidor(preferido: str | None = None) -> str:
    """Intérprete con FastAPI y uvicorn para el servidor."""
    if preferido:
        return preferido
    if os.environ.get("INVEST_PYTHON"):
        return os.environ["INVEST_PYTHON"]
    for candidato in (RAIZ / ".venv" / "bin" / "python", RAIZ / ".venv" / "Scripts" / "python.exe"):
        if candidato.exists():
            return str(candidato)
    return sys.executable


class Servidor:
    """El aplicativo en un proceso aparte, en un puerto libre, con ``INVEST_DATA_DIR`` = ``datos``."""

    def __init__(self, datos: Path, python: str | None = None, puerto: int | None = None, espera: float = 60.0):
        self.datos = Path(datos).resolve()
        self.python = python_servidor(python)
        self.puerto = puerto or puerto_libre()
        self.espera = espera
        self.proceso: subprocess.Popen | None = None
        self.url = f"http://127.0.0.1:{self.puerto}"

    def __enter__(self) -> "Servidor":
        entorno = dict(os.environ)
        entorno["INVEST_DATA_DIR"] = str(self.datos)
        entorno["PYTHONPATH"] = os.pathsep.join([str(RAIZ / "src"), *filter(None, [entorno.get("PYTHONPATH")])])
        entorno["PYTHONUTF8"] = "1"
        codigo = ("import uvicorn; from invest.app.servidor import app, asegurar_base; asegurar_base(); "
                  f"uvicorn.run(app, host='127.0.0.1', port={self.puerto}, log_level='warning')")
        self.proceso = subprocess.Popen([self.python, "-c", codigo], env=entorno, cwd=str(RAIZ),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace")
        limite = time.monotonic() + self.espera
        while time.monotonic() < limite:
            if self.proceso.poll() is not None:
                salida = self.proceso.stdout.read() if self.proceso.stdout else ""
                raise RuntimeError(f"el servidor terminó con código {self.proceso.returncode}:\n{salida}")
            try:
                if self.get("/api/identidad").get("app") == "invest-pipeline":
                    return self
            except OSError:
                pass
            time.sleep(0.2)
        self.__exit__(None, None, None)
        raise RuntimeError(f"el servidor no respondió en {self.espera:.0f} s")

    def __exit__(self, *exc) -> None:
        if self.proceso and self.proceso.poll() is None:
            self.proceso.terminate()
            try:
                self.proceso.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proceso.kill()
        if self.proceso and self.proceso.stdout:
            self.proceso.stdout.close()

    def get(self, ruta: str):
        with urllib.request.urlopen(self.url + ruta, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- páginas
def rutas_con_datos(srv: Servidor, max_fichas: int = 3) -> list[str]:
    """Las páginas fijas más las que llevan parámetro, con especies, carteras y variables reales."""
    rutas = list(PAGINAS_FIJAS)
    try:
        filas = srv.get("/api/screener").get("filas") or []
        especies = [f["especie"] for f in filas if f.get("especie")][:max_fichas]
        activos = srv.get("/api/activos")
        con_puntaje = {f["especie"] for f in filas}
        sin_puntaje = [a["especie"] for a in activos if a["especie"] not in con_puntaje][:1]
        if not especies:
            especies = [a["especie"] for a in activos][:max_fichas]
        rutas += [f"ficha/{urllib.parse.quote(e)}" for e in dict.fromkeys(especies + sin_puntaje)]
        rutas += rutas_graficador(especies)
        if especies:  # Valoración con una especie elegida (#/valoracion/<especie>)
            rutas.append(f"valoracion/{urllib.parse.quote(especies[0])}")
        carteras = srv.get("/api/estado").get("carteras") or []
        rutas += [f"cartera/{urllib.parse.quote(c['cartera_id'])}" for c in carteras]
        variables = srv.get("/api/variables")
        if variables:
            rutas.append(f"aprender/{urllib.parse.quote(variables[0]['id'])}")
    except OSError as e:
        print(f"Aviso: no se pudieron leer los datos para las páginas con parámetro ({e})")
    rutas.append(PAGINA_NEGATIVA)
    return rutas


def rutas_graficador(especies: list[str]) -> list[str]:
    """El graficador con acciones reales: retorno total con el índice del universo (base 100) y P/VL."""
    if not especies:
        return []
    series = ",".join(urllib.parse.quote(e) for e in especies[:4])
    return [f"graficador?series={series},universo_igual&metrica=retorno_total&periodo=5a",
            f"graficador?series={series}&metrica=p_vl&periodo=max"]


def _permitido(ruta_api: str, codigo: int, permitidos: list[tuple[str, int]]) -> bool:
    return any(re.search(patron, ruta_api) and codigo == c for patron, c in permitidos)


ESPERA_JS = """() => {
  const v = document.querySelector('#vista');
  if (!v || !v.children.length) return false;
  return v.textContent.trim() !== 'Cargando…';
}"""

ESTADO_JS = """() => {
  const v = document.querySelector('#vista');
  const error = v.querySelector(':scope > section.panel > h2');
  const h1 = v.querySelector('h1');
  return {
    texto: v.innerText || '',
    error: error && error.textContent === 'Error' ? (v.querySelector('.mensaje.error')?.textContent || 'Error') : null,
    titulo: h1 ? h1.textContent.trim() : '',
    pie: document.querySelector('#pie-lateral')?.textContent || '',
    menu: document.querySelector('#menu a[aria-current=page]')?.dataset.ruta || '',
    cartera: document.querySelector('#vista select[aria-label=Cartera]')?.value ?? null,
  };
}"""


def probar_pagina(contexto, base: str, ruta: str, tiempo: float = 20.0, capturas: Path | None = None) -> Resultado:
    negativa = ruta == PAGINA_NEGATIVA
    permitidos = PERMITIDOS + (PERMITIDOS_NEGATIVA if negativa else [])
    res = Resultado(ruta)
    pagina = contexto.new_page()
    consola: list[str] = []
    malas: list[str] = []
    rechazadas: set[str] = set()

    def al_responder(r):
        partes = urllib.parse.urlsplit(r.url)
        if not partes.path.startswith("/api/"):
            if r.status >= 400 and partes.path.startswith("/static/"):
                malas.append(f"{r.status} {partes.path}")
            return
        res.pedidos_api += 1
        if URL_SOSPECHOSA.search(partes.path + "?" + partes.query):
            malas.append(f"pedido con un valor vacío en la ruta: {r.request.method} {partes.path}"
                         + (f"?{partes.query}" if partes.query else ""))
        if 300 <= r.status < 400:  # la API nunca redirige: /api/macro/ (sin serie) cae en el catálogo
            malas.append(f"{r.status} redirección inesperada {r.request.method} {partes.path}"
                         + (f"?{partes.query}" if partes.query else ""))
        if r.status >= 400:
            if _permitido(partes.path, r.status, permitidos):
                rechazadas.add(r.url)
            else:
                malas.append(f"{r.status} {r.request.method} {partes.path}"
                             + (f"?{partes.query}" if partes.query else ""))

    def al_fallar(req):
        partes = urllib.parse.urlsplit(req.url)
        if partes.path.startswith("/api/") or partes.path.startswith("/static/"):
            malas.append(f"sin respuesta {req.method} {partes.path} ({req.failure})")

    def al_mensaje(m):
        if m.type != "error":
            return
        url = (m.location or {}).get("url", "")
        if m.text.startswith("Failed to load resource") and url in rechazadas:
            return  # el 4xx intencional ya quedó registrado como permitido
        consola.append(m.text)

    pagina.on("response", al_responder)
    pagina.on("requestfailed", al_fallar)
    pagina.on("console", al_mensaje)
    pagina.on("pageerror", lambda e: consola.append(f"excepción: {e}"))
    inicio = time.monotonic()
    try:
        pagina.goto(f"{base}/#/{ruta}", wait_until="domcontentloaded", timeout=tiempo * 1000)
        pagina.wait_for_function(ESPERA_JS, timeout=tiempo * 1000)
        # Gráficos y series que se piden después de pintar (precio, macro): hasta que la red quede quieta
        pagina.wait_for_load_state("networkidle", timeout=tiempo * 1000)
        pagina.wait_for_timeout(150)
        e = pagina.evaluate(ESTADO_JS)
    except Exception as ex:  # noqa: BLE001 - tiempo agotado u otro fallo del navegador
        res.fallar(f"no terminó de cargar: {str(ex).splitlines()[0]}")
        e = None
    res.segundos = time.monotonic() - inicio
    if capturas:
        capturas.mkdir(parents=True, exist_ok=True)
        nombre = re.sub(r"[^\w.-]+", "_", ruta) or "inicio"
        try:
            pagina.screenshot(path=str(capturas / f"{nombre}.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass
    pagina.close()

    for m in malas:
        res.fallar(f"HTTP {m}")
    for m in consola:
        res.fallar(f"consola: {m[:300]}")
    if e is None:
        return res
    res.titulo = e["titulo"]
    if "Sin conexión con el modelo" in e["pie"]:
        res.fallar(f"pie: {e['pie'][:200]}")
    if negativa:
        if not e["error"] or "404" not in e["error"]:
            res.fallar("una especie desconocida debía mostrar el error 404 de la ficha")
        return res
    if e["error"]:
        res.fallar(f"panel de error: {e['error'][:300]}")
    texto = e["texto"].strip()
    if len(texto) < 15 or not e["titulo"]:
        res.fallar(f"página vacía o sin título ({len(texto)} caracteres)")
    if texto == "Cargando…":
        res.fallar("se quedó en Cargando…")
    raiz = ruta.split("?")[0].split("/")[0]
    if e["menu"] != raiz:
        res.fallar(f"el menú marca '{e['menu']}' en vez de '{raiz}'")
    if raiz == "cartera" and "/" in ruta:
        pedida = urllib.parse.unquote(ruta.split("/", 1)[1])
        if e["cartera"] != pedida:
            res.fallar(f"#/{ruta} muestra la cartera '{e['cartera']}' en vez de '{pedida}'")
    sospechosos = sorted(set(SOSPECHOSOS.findall(texto)))
    if sospechosos:
        linea = next((ln for ln in texto.splitlines() if SOSPECHOSOS.search(ln)), "")
        res.fallar(f"texto sospechoso {sospechosos}: «{linea.strip()[:160]}»")
    return res


def probar(datos: Path, python: str | None = None, rutas: list[str] | None = None, tiempo: float = 20.0,
           capturas: Path | None = None, max_fichas: int = 3) -> list[Resultado]:
    """Arranca el servidor con ``datos``, prueba las páginas y devuelve un resultado por página."""
    from playwright.sync_api import sync_playwright

    with Servidor(datos, python) as srv:
        lista = rutas or rutas_con_datos(srv, max_fichas)
        with sync_playwright() as p:
            navegador = p.chromium.launch(headless=True)
            try:
                contexto = navegador.new_context(viewport={"width": 1400, "height": 900}, locale="es-CO")
                return [probar_pagina(contexto, srv.url, r, tiempo, capturas) for r in lista]
            finally:
                navegador.close()


def imprimir(resultados: list[Resultado]) -> None:
    ancho = max(len(r.ruta) for r in resultados) + 3
    for r in resultados:
        marca = "OK   " if r.ok else "FALLA"
        print(f"{marca} #/{r.ruta:<{ancho}} {r.segundos:5.1f} s  {r.pedidos_api:3d} pedidos /api  {r.titulo[:50]}")
        for p in r.problemas:
            print(f"        - {p}")
    malas = sum(not r.ok for r in resultados)
    print(f"\n{len(resultados) - malas} de {len(resultados)} páginas sin problemas" + (f"; {malas} con fallas" if malas else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--datos", default=os.environ.get("INVEST_DATA_DIR") or str(RAIZ.parent / "invest-data"),
                    help="carpeta de datos (INVEST_DATA_DIR); por omisión ../invest-data")
    ap.add_argument("--python", help="intérprete con FastAPI para el servidor (por omisión la .venv del repositorio)")
    ap.add_argument("--paginas", nargs="*", help="solo estas rutas (p. ej. mercado ficha/coc04paao008)")
    ap.add_argument("--tiempo", type=float, default=20.0, help="segundos máximos por página")
    ap.add_argument("--capturas", type=Path, help="carpeta donde guardar una captura PNG de cada página")
    ap.add_argument("--fichas", type=int, default=3, help="cuántas fichas de acciones abrir")
    a = ap.parse_args(argv)
    datos = Path(a.datos)
    if not datos.exists():
        print(f"No existe la carpeta de datos {datos}")
        return 2
    print(f"Datos: {datos.resolve()}")
    try:
        resultados = probar(datos, a.python, a.paginas, a.tiempo, a.capturas, a.fichas)
    except RuntimeError as e:
        print(f"No arrancó el servidor: {e}")
        return 2
    imprimir(resultados)
    return 0 if all(r.ok for r in resultados) else 1


if __name__ == "__main__":
    sys.exit(main())
