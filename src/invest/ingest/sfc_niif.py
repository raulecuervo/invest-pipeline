"""Conector de los estados financieros NIIF de los emisores (SIMEV / RNVE de la SFC), en XBRL.

La página "Informes financieros bajo NIIF y anexos" del SIMEV publica, para cada emisor inscrito y
cada corte trimestral desde 2015, el XBRL del informe separado y del consolidado. La página pide los
archivos a un servicio que exige una clave propia del sitio: esa clave no se replica. Un navegador
real (Playwright) abre la página de cada emisor, elige año, tipo de informe y periodo como lo haría
una persona, pulsa "Ver XBRL" → "Descargar" y se queda con la respuesta que la propia página recibe.

Qué se guarda (``silver/fact_xbrl_sfc``): los hechos numéricos sin dimensiones (las cifras del
estado de situación financiera, de resultados, de flujos de efectivo y las por acción), tanto del
periodo del informe como del periodo comparativo que trae. Cada fila lleva el corte del informe
(``fecha_corte``) y la fecha de transmisión: un mismo periodo reaparece como comparativo en el
informe siguiente, y para evitar sesgo de anticipación el análisis debe usar el dato disponible a
cada fecha. Se prefiere el consolidado; el separado solo si el emisor no transmitió consolidado.

Unidades: la SFC pide los montos en **miles de pesos**; las cifras por acción (``...PerShare``)
están en pesos. Se guarda el valor tal como viene y la columna ``escala`` lo dice.

Los códigos del emisor en el SIMEV (tipo y código de entidad) cambian cuando la SFC lo reclasifica
(p. ej. Grupo Sura pasó de 066-018 a 142-002): ``config/emisores_rnve.yaml`` trae todos los códigos
de cada NIT y el conector prueba cada uno.
"""

from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import polars as pl
import yaml

from invest import storage
from invest.config import CONFIG_DIR
from invest.ingest.base import Conector

URL = "https://www.superfinanciera.gov.co/SIMEV2/rnve/informesfinancierosniif/{tipo}/{codigo:06d}/0"
MESES = {3: "Marzo", 6: "Junio", 9: "Septiembre", 12: "Diciembre"}
INICIO = date(2015, 3, 31)  # la SFC publica XBRL desde el primer trimestre de 2015
FALLOS_SEGUIDOS_MAXIMO = 8  # cortes seguidos que fallan (el SIMEV no responde): se detiene la corrida

XBRLI = "{http://www.xbrl.org/2003/instance}"
PREFIJOS = {  # espacio de nombres → prefijo corto (la versión del año de la taxonomía no importa)
    "xbrl.ifrs.org/taxonomy": "ifrs",
    "superfinanciera.gov.co/xbrl": "sfc",
}

ESQUEMA = {
    "emisor_id": pl.Utf8, "nit": pl.Utf8, "entidad_sfc": pl.Utf8, "fecha_corte": pl.Date,
    "tipo_informe": pl.Utf8, "concepto": pl.Utf8, "tipo_periodo": pl.Utf8, "fecha_inicio": pl.Date,
    "fecha_fin": pl.Date, "valor": pl.Float64, "unidad": pl.Utf8, "escala": pl.Utf8, "radicado": pl.Utf8,
    "fecha_transmision": pl.Date, "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}


# --------------------------------------------------------------------------- configuración y periodos
def emisores() -> list[dict]:
    return yaml.safe_load((CONFIG_DIR / "emisores_rnve.yaml").read_text(encoding="utf-8"))["emisores"]


def cortes(desde: date, hasta: date) -> list[date]:
    """Fines de trimestre entre las dos fechas, del más reciente al más antiguo."""
    salida = []
    for anio in range(max(desde.year, INICIO.year), hasta.year + 1):
        for mes, dia in ((3, 31), (6, 30), (9, 30), (12, 31)):
            c = date(anio, mes, dia)
            if max(desde, INICIO) <= c <= hasta:
                salida.append(c)
    return sorted(salida, reverse=True)


# --------------------------------------------------------------------------- XBRL
def _prefijo(ns: str) -> str | None:
    return next((p for clave, p in PREFIJOS.items() if clave in ns), None)


def hechos_xbrl(contenido: bytes) -> list[dict]:
    """Hechos numéricos sin dimensiones: concepto, periodo, valor y unidad.

    Si un concepto aparece dos veces en el mismo contexto (pasa en algunos informes, uno en cero y otro
    con la cifra), se queda el valor distinto de cero.
    """
    raiz = ET.parse(io.BytesIO(contenido)).getroot()
    contextos: dict[str, tuple[str, date, date]] = {}
    for c in raiz.iter(f"{XBRLI}context"):
        if c.find(f"{XBRLI}scenario") is not None or c.find(f"{XBRLI}entity/{XBRLI}segment") is not None:
            continue
        p = c.find(f"{XBRLI}period")
        if p is None:
            continue
        inst = (p.findtext(f"{XBRLI}instant") or "").strip()
        ini = (p.findtext(f"{XBRLI}startDate") or "").strip()
        fin = (p.findtext(f"{XBRLI}endDate") or "").strip()
        try:
            if inst:
                d = date.fromisoformat(inst[:10])
                contextos[c.get("id")] = ("instante", d, d)
            elif ini and fin:
                contextos[c.get("id")] = ("duracion", date.fromisoformat(ini[:10]), date.fromisoformat(fin[:10]))
        except ValueError:
            continue  # periodo "forever" o fecha ilegible: no es una cifra de un periodo
    elegidos: dict[tuple, dict] = {}
    for e in raiz:
        ctx = e.get("contextRef")
        if ctx not in contextos or not e.tag.startswith("{"):
            continue
        ns, local = e.tag[1:].split("}", 1)
        pref = _prefijo(ns)
        texto = (e.text or "").strip()
        if pref is None or not texto:
            continue
        try:
            valor = float(texto)
        except ValueError:
            continue  # textos y fechas: no son cifras
        tipo, inicio, fin = contextos[ctx]
        clave = (f"{pref}:{local}", tipo, inicio, fin)
        previo = elegidos.get(clave)
        if previo is None or (previo["valor"] == 0 and valor != 0):
            unidad = e.get("unitRef") or ""
            elegidos[clave] = {
                "concepto": clave[0], "tipo_periodo": tipo, "fecha_inicio": inicio, "fecha_fin": fin,
                "valor": valor, "unidad": unidad,
                "escala": _escala(local, unidad),
            }
    return list(elegidos.values())


def _escala(concepto: str, unidad: str) -> str:
    u = unidad.lower()
    if "pershare" in concepto.lower() or "pershare" in u or "accion" in u:
        return "pesos"  # cifra por acción
    if u in ("peso", "pesos", "cop", "iso4217_cop", "iso4217:cop"):
        return "miles_de_pesos"
    return "unidades"


def datos_archivo(nombre: str) -> dict:
    """'0054368214_0260_000036_0000_000000_000000_C-C_2025-12-31.xbrl' → radicado, informe y corte."""
    m = re.match(r"(\d+)_(\d+)_(\d+)_.*_([CI])-([CI])_(\d{4}-\d{2}-\d{2})", nombre)
    if not m:
        return {}
    return {"radicado": m.group(1), "tipo_informe": "consolidado" if m.group(4) == "C" else "separado",
            "fecha_corte": date.fromisoformat(m.group(6))}


# --------------------------------------------------------------------------- anexos
def numero(texto: str | None) -> float | None:
    """'1,765.09' → 1765.09 (formato de EE. UU. del SIMEV); textos → None."""
    t = (texto or "").replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def es_xbrl(estado_http: int, cuerpo: bytes) -> bool:
    """La respuesta de la descarga es un documento XML y no un error del servidor (p. ej. un JSON 500)."""
    if estado_http >= 400:
        return False
    inicio = cuerpo.lstrip()[:5]
    return inicio.startswith(b"<") or inicio.startswith(b"\xef\xbb\xbf<")


def filas_anexos(respuesta: dict, consolidado: bool) -> list[dict]:
    """Aplana la respuesta de anexos del SIMEV: una fila por celda (formato, fila, columna, valor).

    La respuesta trae, por grupo (trimestrales, semestrales), los formatos, los nombres de sus columnas
    y las celdas en orden de lectura; cada formato tiene ``maximoColumnas`` columnas.
    """
    salida = []
    for grupo in ("trimestrales", "semestrales"):
        g = respuesta.get(grupo) or {}
        columnas: dict[str, dict[str, str]] = {}
        for c in g.get("columnasFormatos") or []:
            columnas.setdefault(c["codigoFormato"], {})[str(c["codigoColumna"])] = (c.get("nombreColumna") or "").strip()
        nombres = {f["codigoFormato"]: (f.get("nombre") or "").strip() for f in g.get("formatos") or []}
        fila_actual: dict[str, int] = {}
        concepto_actual: dict[str, str] = {}
        for celda in g.get("detalleFormatos") or []:
            fmt, col = celda["codigoFormato"], str(celda["numColumna"])
            texto = (celda.get("valorColumna") or "").strip()
            if col == "0":
                fila_actual[fmt] = fila_actual.get(fmt, 0) + 1
                concepto_actual[fmt] = texto
                continue
            salida.append({
                "grupo": grupo, "consolidado": consolidado, "formato": fmt, "nombre_formato": nombres.get(fmt, ""),
                "fila": fila_actual.get(fmt, 0), "concepto": concepto_actual.get(fmt, ""),
                "columna": columnas.get(fmt, {}).get(col, col), "valor_texto": texto, "valor": numero(texto),
            })
    return salida


# --------------------------------------------------------------------------- navegador
# "fila" si la tabla de informes NIIF ya muestra el corte pedido; "vacio" si dice que no hay; si no, null
JS_LISTO = """(corte) => {
  const t = document.querySelector('app-root') ? document.querySelector('app-root').innerText : '';
  const i = t.indexOf('Informes financieros bajo NIIF\\n');
  if (i < 0) return null;
  const seccion = t.slice(i, t.indexOf('Anexos', i) > 0 ? t.indexOf('Anexos', i) : i + 3000);
  if (seccion.includes('No hay información reportada')) return 'vacio';
  return seccion.includes(corte) ? 'fila' : null;
}"""


class PaginaSIMEV:
    """Maneja la página del SIMEV como una persona: elige año, tipo de informe y periodo.

    Guarda las respuestas que la propia página recibe: los anexos del corte y, si el emisor transmitió
    XBRL, el archivo que baja al pulsar "Ver XBRL" → "Descargar".
    """

    INICIO_SITIO = "https://www.superfinanciera.gov.co/SIMEV2/rnve"

    def __init__(self, timeout_s: int = 60, visible: bool = False, bitacora: bool = False) -> None:
        self.timeout = timeout_s * 1000
        self.visible = visible
        self.bitacora = bitacora

    def _log(self, texto: str) -> None:
        if self.bitacora:
            print(f"    · {texto}", flush=True)

    def __enter__(self) -> "PaginaSIMEV":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._nav = self._pw.chromium.launch(headless=not self.visible)
        self.pagina = self._nav.new_page(locale="es-CO", viewport={"width": 1366, "height": 900},
                                         accept_downloads=False)
        self.respuestas: list = []
        self.pagina.on("response", self._guardar)
        self.pagina.goto(self.INICIO_SITIO, wait_until="domcontentloaded", timeout=self.timeout)
        return self

    def __exit__(self, *exc) -> None:
        self._nav.close()
        self._pw.stop()

    def _guardar(self, respuesta) -> None:
        if "/sfcservices/SIMEV2/informes-financieros-Niff/" in respuesta.url:
            self.respuestas.append(respuesta)
            self._log(f"respuesta {respuesta.status} {respuesta.url.split('SIMEV2/')[-1][:140]}")

    def _elegir(self, posicion: int, texto: str) -> bool:
        p = self.pagina
        p.locator("button#anioConsulta, button#periodoConsulta").nth(posicion).click()
        opcion = p.locator(".dropdown-menu.show option").filter(has_text=re.compile(rf"^\s*{texto}\s*$"))
        if opcion.count() == 0:
            self._log(f"no aparece la opción {texto!r} en el selector {posicion}")
            p.keyboard.press("Escape")
            return False
        self._log(f"elige {texto!r} en el selector {posicion}")
        opcion.first.click()
        p.wait_for_timeout(700)
        return True

    def _esperar_todas(self, patrones: dict[str, str], ms: int) -> dict:
        """Espera hasta tener una respuesta para cada patrón (o hasta ``ms``).

        Devuelve {clave: bytes del cuerpo}. El cuerpo se lee apenas llega: después de recargar la
        página el navegador ya no lo entrega.
        """
        fin = datetime.now().timestamp() + ms / 1000
        halladas: dict = {}
        while True:
            for clave, patron in patrones.items():
                if clave in halladas:
                    continue
                for r in [x for x in self.respuestas if patron in x.url]:
                    try:
                        if r.ok:
                            halladas[clave] = r.body()
                            break
                    except Exception as e:  # noqa: BLE001 - se prueba con la siguiente respuesta igual
                        self._log(f"no se pudo leer {clave}: {type(e).__name__}")
            if len(halladas) == len(patrones) or datetime.now().timestamp() >= fin:
                return halladas
            self.pagina.wait_for_timeout(300)

    def informe(self, tipo: int, codigo: int, corte: date, nombre: str = "", intentos: int = 3) -> dict | None:
        """{'anexos': [...], 'xbrl': (nombre, bytes, fecha de envío) o None}; None si la página no carga.

        La página no siempre pide los datos en orden (a veces pide los del periodo anterior o no pide
        la lista de informes): se espera hasta tener las respuestas del corte y, si falta alguna, se
        recarga y se elige de nuevo. El XBRL se baja en el mismo intento en que llega la lista, porque
        solo entonces la tabla muestra el botón.
        """
        p = self.pagina
        dmy, ymd = corte.strftime("%d-%m-%Y"), corte.strftime("%Y/%m/%d")
        patrones = {
            "anexos": f"getReporteAnexos?tipoEntidad={tipo}&codigoEntidad={codigo}&periodo={dmy}",
            "consolidados": f"getReporteAnexosConsolidados?tipoEntidad={tipo}&codigoEntidad={codigo}&periodo={dmy}",
            "lista": f"reporte?fechaCorte={ymd}",
        }
        tipo_rep = "Cierre" if corte.month == 12 else "Intermedios"
        halladas: dict = {}
        xbrl, lista_vacia = None, None
        for intento in range(1, intentos + 1):
            self.respuestas.clear()  # las del intento anterior ya no se pueden leer
            p.evaluate("""([t, c, n]) => { sessionStorage.setItem('tipoEntidad', t); sessionStorage.setItem('codigoEntidad', c);
                           sessionStorage.setItem('nombreEntidad', n); }""", [str(tipo), str(codigo), nombre])
            p.goto(URL.format(tipo=tipo, codigo=codigo), wait_until="domcontentloaded", timeout=self.timeout)
            p.locator("button#periodoConsulta").wait_for(timeout=self.timeout)
            p.wait_for_timeout(1000)
            if not (self._elegir(0, str(corte.year)) and self._elegir(1, tipo_rep)
                    and self._elegir(2, MESES[corte.month])):
                return None
            buscar = {k: v for k, v in patrones.items() if k != "lista" and k not in halladas}
            if xbrl is None and lista_vacia is not True:
                buscar["lista"] = patrones["lista"]
            nuevas = self._esperar_todas(buscar, 20000 if intento == 1 else 12000)
            for k in ("anexos", "consolidados"):
                if k in nuevas:
                    halladas[k] = nuevas[k]
            if "lista" in nuevas:
                texto = nuevas["lista"].decode("utf-8", errors="replace").strip()
                lista_vacia = texto in ("", "[]")
                self._log(f"lista de informes: {len(texto)} caracteres")
                if not lista_vacia:
                    xbrl = self._xbrl_en_pantalla(tipo, codigo, corte)
            self._log(f"intento {intento}: anexos {'anexos' in halladas}, consolidados {'consolidados' in halladas}, "
                      f"lista {'vacía' if lista_vacia else 'con informes' if lista_vacia is False else 'sin respuesta'}, "
                      f"XBRL {'sí' if xbrl else 'no'}")
            if "anexos" in halladas and "consolidados" in halladas and (xbrl or lista_vacia):
                break
            if intento >= 2 and "anexos" in halladas and lista_vacia:
                break  # hay emisores sin anexos consolidados: no se insiste
        anexos = []
        for clave, consolidado in (("anexos", False), ("consolidados", True)):
            cuerpo = halladas.get(clave)
            if cuerpo is None:
                continue
            try:
                nuevas_filas = filas_anexos(json.loads(cuerpo.decode("utf-8")), consolidado)
                anexos += nuevas_filas
                self._log(f"{clave}: {len(nuevas_filas)} celdas")
            except Exception as e:  # noqa: BLE001 - un anexo ilegible no tumba el resto
                self._log(f"no se pudo leer {clave}: {type(e).__name__}: {e}")
        return {"anexos": anexos, "xbrl": xbrl, "lista_vista": lista_vacia is not None,
                "xbrl_pendiente": lista_vacia is False and xbrl is None}

    def _xbrl_en_pantalla(self, tipo: int, codigo: int, corte: date) -> tuple[str, bytes, str | None] | None:
        """Baja el XBRL si la tabla ya muestra el corte; None si la página dice que no hay."""
        p = self.pagina
        try:
            p.wait_for_function(JS_LISTO, arg=corte.strftime("%d/%m/%Y"), timeout=15000)
            if p.evaluate(JS_LISTO, corte.strftime("%d/%m/%Y")) != "fila":
                self._log("la tabla dice que no hay informes en esta carga")
                return None
            xbrl = self._bajar_xbrl(tipo, codigo, corte)
            self._log("XBRL: " + (f"{xbrl[0]} ({len(xbrl[1]):,} bytes)" if xbrl else "sin botón activo"))
            return xbrl
        except Exception as e:  # noqa: BLE001 - sin XBRL se conservan los anexos
            self._log(f"XBRL falló: {type(e).__name__}: {str(e)[:200]}")
            p.keyboard.press("Escape")
            return None

    def _bajar_xbrl(self, tipo: int, codigo: int, corte: date) -> tuple[str, bytes, str | None] | None:
        p = self.pagina
        botones = p.locator("button", has_text=re.compile(r"^\s*VER XBRL\s*$"))
        disponibles = [i for i in range(botones.count())
                       if "inactive" not in (botones.nth(i).get_attribute("class") or "")]
        # Orden de la tabla: separado (columna 1) y consolidado (columna 2) de la primera fila
        indice = 1 if 1 in disponibles else (0 if 0 in disponibles else None)
        if indice is None:
            return None
        botones.nth(indice).click()
        dialogo = p.locator("#btnDescargaNiif")
        dialogo.wait_for(timeout=self.timeout)
        envio = None
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", p.locator("mat-dialog-container").first.inner_text(timeout=self.timeout))
        if m:
            envio = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        with p.expect_response(lambda r: "descarga-archivos-niif/descarga" in r.url,
                               timeout=self.timeout * 3) as r:
            dialogo.click()
        respuesta = r.value
        nombre = re.search(r"nombreArchivo=([^&]+)", respuesta.url)
        cuerpo = respuesta.body()
        p.keyboard.press("Escape")
        if not es_xbrl(respuesta.status, cuerpo):
            # El SIMEV a veces responde un error 500 en JSON en vez del archivo: es una descarga fallida,
            # no un XBRL ilegible (queda pendiente y se vuelve a pedir)
            self._log(f"la descarga del XBRL falló (HTTP {respuesta.status}, {len(cuerpo)} bytes)")
            return None
        return (nombre.group(1) if nombre else f"{tipo}_{codigo}_{corte}.xbrl"), cuerpo, envio


# --------------------------------------------------------------------------- conector
ESQUEMA_ANEXOS = {
    "emisor_id": pl.Utf8, "entidad_sfc": pl.Utf8, "fecha_corte": pl.Date, "grupo": pl.Utf8,
    "consolidado": pl.Boolean, "formato": pl.Utf8, "nombre_formato": pl.Utf8, "fila": pl.Int64,
    "concepto": pl.Utf8, "columna": pl.Utf8, "valor_texto": pl.Utf8, "valor": pl.Float64,
    "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}
CLAVES_ANEXOS = ("emisor_id", "fecha_corte", "consolidado", "grupo", "formato", "fila", "columna")


class ConectorNIIF(Conector):
    """Anexos de información económica (``fact_anexo_sfc``, todos los emisores) y XBRL NIIF
    (``fact_xbrl_sfc``, los emisores para los que el SIMEV lo publica) del SIMEV."""

    fuente_id = "sfc_rnve_niif"
    tabla = "fact_anexo_sfc"
    claves = CLAVES_ANEXOS
    columna_fecha = "fecha_corte"
    claves_xbrl = ("emisor_id", "tipo_informe", "concepto", "fecha_inicio", "fecha_fin", "fecha_corte")

    def __init__(self, pagina_factory=PaginaSIMEV) -> None:
        super().__init__()
        self._pagina_factory = pagina_factory

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        hoy = date.today()
        # Sin --desde se revisan todos los cortes desde 2015 que falten (la carga inicial se completa en
        # varias corridas, de lo más reciente a lo más antiguo); con --desde se vuelve a pedir ese rango.
        inicio = desde if self.forzado else INICIO
        tope = min(hasta, hoy - timedelta(days=int(self.meta.get("dias_tras_corte", 30))))
        maximo = int(self.meta.get("max_informes_por_corrida", 40))
        estado = self.estado_nuevo.setdefault("informes", {})
        pendientes = []
        for corte in cortes(inicio, tope):
            for em in emisores():
                clave = f"{em['emisor_id']}|{corte}"
                previo = estado.get(clave)
                reciente = (hoy - corte).days < 200
                xbrl_por_bajar = previo and previo.get("xbrl_pendiente") and int(previo.get("intentos_xbrl", 0)) < 3
                if previo and not self.forzado and not xbrl_por_bajar and (
                        previo.get("anexos") or previo.get("radicado") or not reciente):
                    continue  # ya está, o no hubo nada y el corte es viejo (no se reintenta)
                pendientes.append((corte, em, clave))
        # Orden: el último año (cifras vigentes), luego los cierres anuales hacia atrás y al final los
        # demás trimestres; dentro de cada grupo, del corte más reciente al más antiguo
        pendientes.sort(key=lambda t: (0 if (hoy - t[0]).days <= 400 else 1 if t[0].month == 12 else 2,
                                       -t[0].toordinal()))
        hechos: list[dict] = []
        anexos: list[dict] = []
        errores: list[str] = []
        if not pendientes:
            self.sin_novedades = True
            return anexos
        vacios = con_xbrl = seguidos = 0
        detenida = None
        lote = pendientes[:maximo]
        print(f"  SIMEV: {len(lote)} cortes por revisar (de {len(pendientes)} pendientes)", flush=True)
        with self._pagina_factory() as pagina:
            for n, (corte, em, clave) in enumerate(lote, 1):
                encontrado, entidad, nit, fallo = None, "", str(em["nit"]), False
                for codigo in em["codigos"]:
                    try:
                        r = pagina.informe(int(codigo["tipo"]), int(codigo["codigo"]), corte, em.get("nombre", ""))
                    except Exception as e:  # noqa: BLE001 - un emisor que falla no detiene la corrida
                        errores.append(f"{em['emisor_id']} {corte}: {type(e).__name__}")
                        r, fallo = None, True
                    if r and (r["anexos"] or r["xbrl"]):
                        encontrado = r
                        entidad = f"{codigo['tipo']}-{codigo['codigo']}"
                        nit = str(codigo.get("nit", em["nit"]))
                        break
                print(f"  [{n}/{len(lote)}] {em.get('nombre', em['emisor_id'])} {corte}: "
                      + ("error" if fallo and not encontrado else
                         f"anexos {len(encontrado['anexos'])}, XBRL {'sí' if encontrado['xbrl'] else 'no'}" if encontrado
                         else "sin nada"), flush=True)
                if not encontrado and fallo:
                    # No se marca: se vuelve a intentar. Si el SIMEV deja de responder (varias fallas seguidas),
                    # se detiene la corrida en vez de seguir insistiendo; lo que falta queda pendiente
                    seguidos += 1
                    if seguidos >= FALLOS_SEGUIDOS_MAXIMO:
                        detenida = n
                        print(f"  SIMEV: {seguidos} fallas seguidas; se detiene la corrida", flush=True)
                        break
                    continue
                seguidos = 0
                if not encontrado:
                    estado[clave] = {"radicado": None, "anexos": 0, "revisado": hoy.isoformat()}
                    vacios += 1
                    continue
                try:
                    for a in encontrado["anexos"]:
                        anexos.append({**a, "emisor_id": em["emisor_id"], "entidad_sfc": entidad, "fecha_corte": corte})
                    radicado, xbrl_ilegible = None, False
                    if encontrado["xbrl"]:
                        nombre, cuerpo, envio = encontrado["xbrl"]
                        self.crudos[nombre] = cuerpo
                        datos = datos_archivo(nombre)
                        radicado = datos.get("radicado")
                        try:
                            lista_hechos = hechos_xbrl(cuerpo)
                            con_xbrl += 1
                        except Exception as e:  # noqa: BLE001 - un XBRL ilegible no tumba la corrida
                            # Los anexos sí se guardan; el XBRL queda pendiente y se vuelve a bajar (hasta 3 veces)
                            errores.append(f"{em['emisor_id']} {corte} XBRL ilegible: {type(e).__name__}")
                            lista_hechos, xbrl_ilegible = [], True
                        if em.get("moneda_informes", "COP") == "USD":
                            for h in lista_hechos:
                                if h["escala"] == "miles_de_pesos":
                                    h["escala"] = "miles_de_dolares"
                                elif h["escala"] == "pesos":
                                    h["escala"] = "dolares"
                        for h in lista_hechos:
                            hechos.append({
                                **h, "emisor_id": em["emisor_id"], "nit": nit, "entidad_sfc": entidad,
                                "fecha_corte": datos.get("fecha_corte", corte),
                                "tipo_informe": datos.get("tipo_informe", "consolidado"), "radicado": radicado,
                                "fecha_transmision": date.fromisoformat(envio) if envio else None,
                            })
                    previo = estado.get(clave) or {}
                    xbrl_pendiente = bool(encontrado.get("xbrl_pendiente")) or xbrl_ilegible
                    estado[clave] = {"radicado": radicado, "anexos": len(encontrado["anexos"]), "revisado": hoy.isoformat(),
                                     "xbrl_pendiente": xbrl_pendiente, "xbrl_ilegible": xbrl_ilegible,
                                     "intentos_xbrl": int(previo.get("intentos_xbrl", 0)) + (1 if xbrl_pendiente else 0)}
                except Exception as e:  # noqa: BLE001 - un emisor raro no tumba la corrida
                    errores.append(f"{em['emisor_id']} {corte}: {type(e).__name__}: {str(e)[:80]}")
        if hechos:
            ahora = datetime.now(timezone.utc)
            df = pl.DataFrame([{**h, "fuente_id": self.fuente_id, "ingest_ts": ahora} for h in hechos], schema=ESQUEMA)
            df = df.unique(subset=list(self.claves_xbrl), keep="last", maintain_order=True)
            df = df.filter(pl.col("fecha_fin") <= pl.col("fecha_corte"))
            storage.upsert_por_anio("silver", "fact_xbrl_sfc", df, self.claves_xbrl, "fecha_corte")
            self.notas.append(f"hechos XBRL {df.height} en fact_xbrl_sfc")
        leidos = detenida or min(len(pendientes), maximo)
        if detenida:
            self.notas.append(f"corrida detenida tras {FALLOS_SEGUIDOS_MAXIMO} fallas seguidas del SIMEV (corte {detenida} "
                              f"de {len(lote)}); se retoma en la próxima")
        self.notas.append(f"cortes revisados {leidos}: con anexos o XBRL {leidos - vacios}, con XBRL {con_xbrl}, "
                          f"sin nada {vacios}; pendientes para otra corrida {len(pendientes) - leidos}")
        if errores:
            self.notas.append(f"errores {len(errores)} (se reintentan en la próxima corrida; un XBRL ilegible, hasta 3 "
                              "veces): " + ", ".join(errores[:5]))
        if anexos:
            self.crudos["anexos.json"] = json.dumps(anexos, ensure_ascii=False, default=str).encode("utf-8")
        return anexos

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA_ANEXOS)
        ahora = datetime.now(timezone.utc)
        df = pl.DataFrame([{**r, "fuente_id": self.fuente_id, "ingest_ts": ahora} for r in registros],
                          schema=ESQUEMA_ANEXOS)
        return df.unique(subset=list(self.claves), keep="last", maintain_order=True)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        # Las celdas de texto (fechas, nombres de accionistas) se guardan con valor vacío: no son error
        return df, df.clear().with_columns(pl.lit(None, pl.Utf8).alias("motivo"))
