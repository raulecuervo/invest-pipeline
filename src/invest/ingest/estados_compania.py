"""Estados financieros consolidados publicados por cada compañía (PDF), leídos con dos agentes.

Respaldo de los emisores sin XBRL en el SIMEV (``config/estados_compania.yaml``). Cada corrida:

1. **Descubre** PDF nuevos: enlaces de las páginas índice (descarga condicional; también las
   direcciones ``.pdf`` que la página trae dentro de su código), plantillas de URL por corte (p. ej.
   Éxito ``consolidados/<t>t<año>.pdf``) y archivos puestos a mano en
   ``invest-data/fuentes_compania/<emisor_id>/`` (sitios con protección contra robots, que no se
   esquiva). Para los emisores con ``estados_desde_sfc: true`` también toma los estados que
   transmitieron como información relevante a la SFC (informe periódico trimestral o de fin de
   ejercicio), que baja ``sfc_informacion_relevante`` a ``invest-data/anexos/sfc/``. Se filtran con
   los patrones del emisor y se deja uno por corte (el de más prioridad: a mano, sitio de la
   compañía, SFC consolidado, SFC separado).
2. **Baja solo lo nuevo**: una URL ya guardada no se vuelve a pedir (salvo ``--desde``); un 404 se
   reintenta cada ``reintentar_404_dias``; un 401/403 o un desafío contra robots detiene ese emisor
   en la corrida y queda anotado. Los PDF van a ``invest-data/anexos/companias/<emisor_id>/``
   (repositorio privado; ``redistribuible: false``).
3. **Ubica las páginas** del estado de situación financiera y del estado de resultados (y, si cabe,
   del de cambios en el patrimonio) con el texto del PDF, y arma un PDF solo con esas páginas
   (``max_paginas_por_documento``). Así cada lectura cuesta de 4 a 8 páginas y no de 40 a 300.
4. **Dos agentes y consenso**, como ``invest.dividendos``: lectura independiente con modelos e
   instrucciones distintos, verificación de cada cifra contra el texto de las páginas enviadas,
   revisión cruzada hasta ``rondas_maximas`` y resultado ``consenso`` o ``en_disputa``. Las lecturas
   quedan en ``silver/fact_ee_compania_extraccion`` (para auditar y para no volver a pagar).
5. **Controles**: activos = pasivos + patrimonio_total (± 1 %), patrimonio_controladora ≤
   patrimonio_total, signos (activos, ingresos y efectivo no negativos; efectivo ≤ activos; utilidad
   de la controladora y utilidad por acción con el mismo signo), moneda esperada, corte de fin de
   trimestre y resultados acumulados del año (3, 6, 9 o 12 meses según el corte).
6. **Silver** ``fact_ee_compania``: una fila por emisor × corte × concepto con valor en pesos (o
   ``usd``; la utilidad por acción en ``pesos_por_accion`` / ``usd_por_accion``), archivo, página,
   fecha de publicación detectada y estado. Lo que no tuvo consenso o no pasa un control va a
   cuarentena con su motivo.

Sin ``ANTHROPIC_API_KEY`` el paso de lectura se omite (los PDF quedan bajados y pendientes). Las
notas de la corrida solo traen conteos, nunca cifras.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

import polars as pl
import yaml

from invest import dividendos, eventos, storage
from invest.config import CONFIG_DIR, data_dir
from invest.ingest import http
from invest.ingest.base import Conector

FUENTE_ID = "estados_compania"
TABLA = "fact_ee_compania"
TABLA_EXTRACCION = "fact_ee_compania_extraccion"
CARPETA_MANUAL = "fuentes_compania"
CARPETA_ANEXOS = "companias"
REINTENTAR = {"error", "sin_paginas", "sin_texto"}

CONCEPTOS = ["activos", "pasivos", "patrimonio_total", "patrimonio_controladora", "efectivo",
             "ingresos_consolidados", "utilidad_total", "utilidad_controladora", "upa_basica",
             # Desde la versión 2 de la lectura (24-nov-2026): EBITDA, márgenes y deuda
             "utilidad_bruta", "utilidad_operacional", "depreciacion_amortizacion",
             "obligaciones_financieras_corrientes", "obligaciones_financieras_no_corrientes",
             "titulos_emitidos_corrientes", "titulos_emitidos_no_corrientes"]
DEUDA = {"obligaciones_financieras_corrientes", "obligaciones_financieras_no_corrientes",
         "titulos_emitidos_corrientes", "titulos_emitidos_no_corrientes"}
BALANCE = {"activos", "pasivos", "patrimonio_total", "patrimonio_controladora", "efectivo"} | DEUDA
RESULTADOS = {"ingresos_consolidados", "utilidad_total", "utilidad_controladora", "upa_basica",
              "utilidad_bruta", "utilidad_operacional", "depreciacion_amortizacion"}
# Un documento leído con una versión anterior se vuelve a leer (dentro del tope de documentos por corrida)
VERSION_LECTURA = 2
POR_ACCION = {"upa_basica"}
FACTOR = {"pesos": 1.0, "miles": 1e3, "millones": 1e6, "miles_de_millones": 1e9}
MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
            "octubre", "noviembre", "diciembre"]
MESES_EN = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
            "october", "november", "december"]


def config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "estados_compania.yaml").read_text(encoding="utf-8"))


def emisores_config(cfg: dict | None = None) -> dict[str, dict]:
    return {e["emisor_id"]: e for e in (cfg or config())["emisores"]}


def solo_cotejo() -> set[str]:
    """Emisores de control (tienen XBRL): sus lecturas se cotejan pero no entran a fact_fundamental."""
    return {e for e, c in emisores_config().items() if c.get("solo_cotejo")}


# --------------------------------------------------------------------------- estructura pedida
_CIFRA = {
    "type": "object",
    "properties": {
        "valor": {"type": ["number", "null"], "description": "cifra tal como aparece, en las unidades del documento; negativa si va entre paréntesis"},
        "pagina": {"type": ["integer", "null"], "description": "página del PDF adjunto (desde 1)"},
        "rotulo": {"type": ["string", "null"], "description": "nombre de la línea, copiado del documento"},
    },
    "required": ["valor", "pagina", "rotulo"],
}
ESQUEMA = {
    "type": "object",
    "properties": {
        "es_consolidado": {"type": "boolean", "description": "true si son estados consolidados (la compañía y sus subordinadas)"},
        "fecha_corte": {"type": ["string", "null"], "description": "AAAA-MM-DD del estado de situación financiera del periodo del informe"},
        "moneda": {"type": "string", "enum": ["COP", "USD", "otra"]},
        "unidades": {"type": "string", "enum": ["pesos", "miles", "millones", "miles_de_millones"],
                     "description": "unidad de los montos (no de la utilidad por acción)"},
        "meses_acumulados": {"type": ["integer", "null"], "description": "meses que cubre la columna de resultados registrada (3, 6, 9 o 12)"},
        "fecha_informe_revisor": {"type": ["string", "null"], "description": "AAAA-MM-DD del informe o la revisión del revisor fiscal, si aparece"},
        "cifras": {"type": "object", "properties": {c: _CIFRA for c in CONCEPTOS}, "required": CONCEPTOS},
        "observaciones": {"type": "string"},
    },
    "required": ["es_consolidado", "fecha_corte", "moneda", "unidades", "meses_acumulados", "cifras", "observaciones"],
}
HERRAMIENTA = {
    "name": "registrar_estados_financieros",
    "description": "Registra las cifras de los estados financieros consolidados del periodo del informe, tal como están escritas.",
    "input_schema": ESQUEMA,
}

REGLAS = """Reglas:
- Usa solo cifras escritas en el documento; no calcules, no sumes ni restes. Si un concepto no
  aparece como línea de los estados adjuntos, déjalo en null.
- Toma solo la columna del periodo del informe (el corte más reciente), nunca la del comparativo
  (año anterior o 31 de diciembre anterior).
- En el estado de resultados toma la columna ACUMULADA del año (p. ej. "del 1 de enero al 30 de
  junio", "seis meses", "nueve meses"), no la del trimestre solo, y di en meses_acumulados cuántos
  meses cubre (en un cierre anual, 12).
- Registra los montos tal como aparecen, en la unidad que declara el documento ("cifras expresadas
  en millones de pesos" → unidades = millones; "miles de millones" → miles_de_millones), sin
  convertir y sin separadores de miles ("37.636.229" se registra 37636229). Las cifras entre
  paréntesis son negativas. La utilidad por acción va tal como aparece (normalmente en pesos o
  dólares por acción, sin escalar).
- es_consolidado = true solo si los estados son consolidados. Si el documento trae separados y
  consolidados, usa solo los consolidados.
- Conceptos: activos = total activos; pasivos = total pasivos; patrimonio_total = total patrimonio
  (con las participaciones no controladoras); patrimonio_controladora = patrimonio atribuible a los
  propietarios de la controladora (puede estar en el estado de cambios en el patrimonio);
  efectivo = efectivo y equivalentes de efectivo; ingresos_consolidados = ingresos de actividades
  ordinarias (ventas, ingresos operacionales o de contratos con clientes; si hay varias líneas, su
  total); utilidad_total = resultado neto del periodo (utilidad o pérdida); utilidad_controladora =
  la parte atribuible a los propietarios de la controladora; upa_basica = utilidad básica por acción
  (la total; si solo aparece la de operaciones continuas, esa); utilidad_bruta = utilidad (ganancia)
  bruta, si el estado de resultados la trae; utilidad_operacional = utilidad (ganancia) operacional o
  "resultado de actividades de operación", antes de ingresos y gastos financieros; depreciacion_amortizacion
  = depreciación y amortización del estado de flujos de efectivo (la línea de ajuste a la utilidad; si
  vienen en dos líneas, depreciación y amortización por separado, deja null: no las sumes);
  obligaciones_financieras_corrientes y obligaciones_financieras_no_corrientes = las líneas de
  obligaciones financieras, préstamos u "otros pasivos financieros" del pasivo corriente y no corriente;
  titulos_emitidos_corrientes y titulos_emitidos_no_corrientes = bonos o títulos emitidos, solo si el
  balance los muestra en líneas propias, aparte de las obligaciones financieras. No incluyas pasivos por
  arrendamientos.
- pagina: número de página del PDF adjunto (empieza en 1); rotulo: nombre de la línea copiado.
Registra el resultado con la herramienta registrar_estados_financieros."""

INSTRUCCIONES = {
    "a": ("Eres un analista financiero que lee estados financieros de emisores colombianos publicados "
          "en sus sitios de inversionistas. Extrae las cifras consolidadas del periodo del informe.\n" + REGLAS),
    "b": ("Eres un auditor que verifica estados financieros. Antes de registrar, ubica el encabezado "
          "de cada estado (entidad, si es consolidado, corte, unidades y moneda), identifica qué columna "
          "es la del periodo del informe y cuál la acumulada del año, y comprueba que total activos = "
          "total pasivos + total patrimonio y que el patrimonio total = el de la controladora + las "
          "participaciones no controladoras. Si no cuadra, vuelve a leer antes de registrar.\n" + REGLAS),
}


INSTRUCCIONES["arbitro"] = (
    "Eres el árbitro entre dos lectores de estados financieros que no se pusieron de acuerdo en algunas "
    "cifras. No elijas entre sus propuestas por confianza: lee tú mismo el documento adjunto, ubica cada "
    "estado (situación financiera, resultados, flujos), su encabezado (entidad, consolidado o separado, "
    "corte, unidades, moneda) y la columna del periodo del informe, y registra todo el documento como lo "
    "harías desde cero. Copia cada cifra tal como aparece impresa. Si una cifra no está en las páginas "
    "adjuntas, déjala en null; no la calcules.\n" + REGLAS)


@dataclass
class AgenteEE(dividendos.Agente):
    """Agente de dividendos con la instrucción y los pedidos de los estados financieros."""

    def extraer(self, pdf: bytes, contexto: str) -> dict:
        texto = f"{contexto}\n\nExtrae del documento adjunto las cifras consolidadas del periodo del informe."
        return self.cliente.registrar(self.modelo, INSTRUCCIONES[self.clave], pdf, texto)

    def revisar(self, pdf: bytes, contexto: str, propio: dict, otro: dict, diferencias: list[str]) -> dict:
        return self.cliente.registrar(self.modelo, INSTRUCCIONES[self.clave], pdf,
                                      dividendos.texto_revision(contexto, propio, otro, diferencias))


# --------------------------------------------------------------------------- texto y fechas
def normalizar(texto: str | None) -> str:
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", t)


def fin_de_trimestre(anio: int, t: int) -> date:
    return {1: date(anio, 3, 31), 2: date(anio, 6, 30), 3: date(anio, 9, 30), 4: date(anio, 12, 31)}[t]


def trimestre_anterior(d: date) -> date:
    """Último fin de trimestre estrictamente anterior a ``d``."""
    for anio in (d.year, d.year - 1):
        for t in (4, 3, 2, 1):
            f = fin_de_trimestre(anio, t)
            if f < d:
                return f
    raise ValueError(d)


def _anio(texto: str) -> int:
    n = int(texto)
    return n + 2000 if n < 100 else n


def fecha_de_ruta(url: str) -> date | None:
    """Mes de la carpeta de carga (``.../uploads/2026/05/...``), primer día del mes."""
    m = re.search(r"/(20\d\d)/(0[1-9]|1[0-2])/", url)
    return date(int(m.group(1)), int(m.group(2)), 1) if m else None


def corte_probable(url: str, texto: str = "") -> date | None:
    """Corte que sugiere el nombre del archivo, el texto del enlace o la carpeta de carga."""
    nombre = normalizar(unquote(urlparse(url).path.rsplit("/", 1)[-1]) + " " + (texto or ""))
    nombre = re.sub(r"[-_.]", " ", nombre)
    m = (re.search(r"(?<![0-9])([1-4])\s*[tq]\s*(20\d\d|\d\d)(?![0-9])", nombre)
         or re.search(r"\bq([1-4])\s*(20\d\d|\d\d)(?![0-9])", nombre))
    if m:
        return fin_de_trimestre(_anio(m.group(2)), int(m.group(1)))
    meses = "|".join(MESES_ES + MESES_EN)
    m = re.search(rf"\b({meses})\b(?:\s+de)?\s+(20\d\d)\b", nombre)
    if m:
        mes = (MESES_ES.index(m.group(1)) if m.group(1) in MESES_ES else MESES_EN.index(m.group(1))) + 1
        if mes in (3, 6, 9, 12):
            return fin_de_trimestre(int(m.group(2)), mes // 3)
    carpeta = fecha_de_ruta(url)
    m = re.search(rf"\b({meses})\b", nombre)
    if m and carpeta:  # "Consolidados-Junio.pdf" en la carpeta 2025/07: el junio anterior a la carga
        mes = (MESES_ES.index(m.group(1)) if m.group(1) in MESES_ES else MESES_EN.index(m.group(1))) + 1
        if mes in (3, 6, 9, 12):
            anio = carpeta.year if mes < carpeta.month else carpeta.year - 1
            return fin_de_trimestre(anio, mes // 3)
    m = re.search(r"(?<![0-9])(20\d\d)(?![0-9])", nombre)
    if m and not re.search(r"(?<![0-9])(20\d\d)\s+(?:y\s+|and\s+)?(20\d\d)(?![0-9])", nombre):  # "Consolidados 2025" = cierre anual
        return date(int(m.group(1)), 12, 31)
    return trimestre_anterior(carpeta) if carpeta else None


def fecha_publicacion(url: str, last_modified: str | None) -> tuple[date | None, str | None]:
    """(fecha, origen): ``Last-Modified`` del servidor; si no, el último día del mes de la carpeta de
    carga (conservador); si no, nada (se usa el plazo legal en fundamentales)."""
    if last_modified:
        try:
            return parsedate_to_datetime(last_modified).date(), "last_modified"
        except (TypeError, ValueError):
            pass
    carpeta = fecha_de_ruta(url)
    if carpeta:
        siguiente = date(carpeta.year + (carpeta.month == 12), carpeta.month % 12 + 1, 1)
        return siguiente - timedelta(days=1), "carpeta"
    return None, None


# --------------------------------------------------------------------------- descubrimiento
class _Anclas(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.enlaces: list[tuple[str, str]] = []
        self._href: str | None = None
        self._texto: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._texto = []

    def handle_data(self, data):
        if self._href is not None:
            self._texto.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.enlaces.append((self._href, " ".join("".join(self._texto).split())))
            self._href = None


def enlaces(html: str, base: str) -> list[tuple[str, str]]:
    """(URL absoluta, texto) de los enlaces de la página y de las direcciones .pdf que trae en su
    código (muchas páginas arman los botones con JavaScript a partir de un JSON)."""
    lector = _Anclas()
    try:
        lector.feed(html)
    except Exception:  # HTML mal formado: quedan las direcciones sueltas
        pass
    vistos: dict[str, str] = {}
    for href, texto in lector.enlaces:
        if href and not href.startswith(("#", "mailto:", "javascript:")):
            vistos.setdefault(urljoin(base, href.strip()), texto)
    crudo = html.replace("\\/", "/")
    for m in re.finditer(r"""(https?://[^\s"'<>()]+?\.pdf|/[^\s"'<>()]+?\.pdf)(?=[\s"'<>?#)]|$)""", crudo, flags=re.I):
        vistos.setdefault(urljoin(base, m.group(1)), "")
    return list(vistos.items())


def candidatos(emisor: dict, lista: list[tuple[str, str]]) -> list[dict]:
    """Enlaces que son estados consolidados del emisor según sus patrones (en orden de preferencia)."""
    patrones = [re.compile(p, re.I) for p in emisor.get("patrones") or []]
    incluir = [re.compile(p, re.I) for p in emisor.get("incluir") or []]
    excluir = [re.compile(p, re.I) for p in emisor.get("excluir") or []]
    salida = []
    for url, texto in lista:
        cadena = normalizar(unquote(url)) + " " + normalizar(texto)
        if not url.lower().split("?")[0].endswith(".pdf"):
            continue
        prioridad = next((i for i, p in enumerate(patrones) if p.search(cadena)), None)
        if prioridad is None or not all(p.search(cadena) for p in incluir) or any(p.search(cadena) for p in excluir):
            continue
        salida.append({"url": url, "texto": texto, "prioridad": prioridad, "corte": corte_probable(url, texto)})
    return salida


def urls_plantilla(emisor: dict, hoy: date, trimestres: int, dias_tras_corte: int) -> list[dict]:
    """URLs de las plantillas para los últimos cortes cuyo plazo de publicación ya empezó."""
    salida = []
    corte = trimestre_anterior(hoy - timedelta(days=dias_tras_corte - 1))
    cortes = []
    while len(cortes) < trimestres:
        cortes.append(corte)
        corte = trimestre_anterior(corte)
    for i, plantilla in enumerate(emisor.get("plantillas") or []):
        for c in cortes:
            t = (c.month - 1) // 3 + 1
            mes = MESES_ES[c.month - 1]
            url = plantilla.format(t=t, anio=c.year, aa=f"{c.year % 100:02d}", mm=f"{c.month:02d}",
                                   mes_es=mes, Mes_es=mes.capitalize(), MES_ES=mes.upper())
            salida.append({"url": url, "texto": "", "prioridad": i, "corte": c})
    return salida


def es_pdf(contenido: bytes | None) -> bool:
    return bool(contenido) and contenido.lstrip()[:5] == b"%PDF-"


MARCAS_BLOQUEO = (b"_incapsula_resource", b"incapsula incident", b"cf-chl", b"challenge-platform")
MARCAS_GENERICAS = (b"captcha", b"request blocked", b"access denied")


def es_bloqueo(contenido: bytes | None) -> bool:
    """Página de una protección contra robots en lugar del PDF (no se intenta esquivar). Las marcas
    genéricas solo cuentan en páginas cortas, para no confundir una página normal que las mencione."""
    muestra = (contenido or b"")[:20000].lower()
    return any(m in muestra for m in MARCAS_BLOQUEO) or (
        len(contenido or b"") < 30000 and any(m in muestra for m in MARCAS_GENERICAS))


def pdf_de_zip(contenido: bytes) -> bytes | None:
    """El PDF de estados financieros dentro de un ZIP (los anexos de la SFC a veces vienen así): el que
    dice consolidado, si no el que dice estados financieros, si no el más grande."""
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(contenido))
    except zipfile.BadZipFile:
        return None
    pdfs = [i for i in z.infolist() if i.filename.lower().endswith(".pdf") and not i.is_dir()]
    if not pdfs:
        return None

    def orden(i):
        n = normalizar(i.filename)
        return ("consolidad" not in n, not re.search(r"estado|eeff|financier", n), -i.file_size)
    datos = z.read(min(pdfs, key=orden))
    return datos if es_pdf(datos) else None


def _nombre_archivo(url: str) -> str:
    nombre = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "documento.pdf"
    nombre = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    nombre = re.sub(r"[^A-Za-z0-9._-]+", "_", nombre)[-120:]
    return nombre if nombre.lower().endswith(".pdf") else nombre + ".pdf"


# --------------------------------------------------------------------------- páginas
def textos_paginas(pdf: bytes) -> list[str]:
    try:
        from pypdf import PdfReader

        return [p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages]
    except Exception:
        return []


def numero_paginas(pdf: bytes) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(pdf)).pages)
    except Exception:
        return None


def _densidad(texto: str) -> int:
    """Cantidad de cifras con separador de miles o de 4 dígitos o más (una página de estados tiene decenas)."""
    return len(re.findall(r"\(?\d{1,3}(?:[.,]\d{3})+\)?|\b\d{4,}\b", texto))


def _contiene(texto: str, frases: list[str]) -> bool:
    return any(normalizar(f) in texto for f in frases)


_CALIFICATIVOS = re.compile(r"\b(condensad[oa]s?|condesad[oa]s?|intermedi[oa]s?|resumid[oa]s?)\b ?")


def _sin_calificativos(t: str) -> str:
    """También "total, activo" → "total activo" (Paz del Río escribe así sus totales)."""
    return re.sub(r"  +", " ", re.sub(r"\btotal\s*,\s*", "total ", _CALIFICATIVOS.sub("", t)))


def localizar_paginas(textos: list[str], cfg: dict) -> list[int]:
    """Índices (desde 0) de las páginas de los estados principales, como máximo
    ``max_paginas_por_documento``: situación financiera y resultados (consolidados si los hay) y, si
    queda espacio, flujos de efectivo (por la depreciación) y cambios en el patrimonio."""
    maximo = int(cfg.get("limites", {}).get("max_paginas_por_documento", 8))
    # "Estado condensado de resultados", "estado de situación financiera condesado [sic]" (Paz del Río,
    # jun-2026): los calificativos de los intermedios no deben impedir reconocer el título
    norm = [_sin_calificativos(normalizar(t)) for t in textos]
    otros_titulos = [t for clase in cfg["paginas"].values() for t in clase["titulos"]] + [
        "flujos de efectivo", "flujo de efectivo", "cash flows", "otro resultado integral"]
    elegidas: list[int] = []
    for clase in ("situacion", "resultados", "flujos", "patrimonio"):
        if clase not in cfg["paginas"]:
            continue
        reglas = cfg["paginas"][clase]
        mejor, puntaje = None, None
        for i, t in enumerate(norm):
            if i in elegidas or _densidad(t) < 12 or not _contiene(t, reglas["titulos"]) or not _contiene(t, reglas["claves"]):
                continue
            if re.search(r"\b(indice|contenido|table of contents)\b", t[:300]) or t.count("....") > 5:
                continue
            p = (3 if "consolidad" in t else 0) - (3 if ("separad" in t or "individual" in t) and "consolidad" not in t else 0)
            if puntaje is None or p > puntaje:
                mejor, puntaje = i, p
        if mejor is None:
            continue
        bloque = [mejor]
        j = mejor
        while (len(bloque) <= int(reglas.get("paginas_extra", 0)) and not _contiene(norm[j], reglas.get("cierre") or [])
               and j + 1 < len(norm) and _densidad(norm[j + 1]) >= 12 and j + 1 not in elegidas):
            siguiente = norm[j + 1]
            titulo_ajeno = any(normalizar(x) in siguiente[:400] for x in otros_titulos
                               if not any(normalizar(x) == normalizar(y) for y in reglas["titulos"]))
            if titulo_ajeno:
                break
            j += 1
            bloque.append(j)
        if clase in ("flujos", "patrimonio") and len(elegidas) + len(bloque) > maximo:
            continue
        elegidas += bloque
    return sorted(elegidas)[:maximo]


def unir_pdf(partes: list[bytes]) -> bytes:
    if len(partes) == 1:
        return partes[0]
    from pypdf import PdfReader, PdfWriter

    escritor = PdfWriter()
    for p in partes:
        for pagina in PdfReader(io.BytesIO(p)).pages:
            escritor.add_page(pagina)
    salida = io.BytesIO()
    escritor.write(salida)
    return salida.getvalue()


def recortar(pdf: bytes, indices: list[int]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    lector = PdfReader(io.BytesIO(pdf))
    escritor = PdfWriter()
    for i in indices:
        escritor.add_page(lector.pages[i])
    salida = io.BytesIO()
    escritor.write(salida)
    return salida.getvalue()


# --------------------------------------------------------------------------- comparación
def _num(v) -> float | None:
    return dividendos._num(v)


def _cifras(r: dict) -> dict[str, float | None]:
    c = r.get("cifras") or {}
    return {k: _num((c.get(k) or {}).get("valor")) for k in CONCEPTOS}


def normalizado(r: dict, concepto: str) -> float | None:
    v = _cifras(r)[concepto]
    if v is None:
        return None
    return v if concepto in POR_ACCION else v * FACTOR.get(r.get("unidades"), float("nan"))


def comparar(a: dict, b: dict) -> list[str]:
    """Diferencias que impiden el consenso. Montos iguales si difieren en menos de media unidad del
    documento (redondeo); utilidad por acción, en menos de 0,01."""
    dif = []
    for campo in ("es_consolidado", "fecha_corte", "moneda", "unidades", "meses_acumulados"):
        if a.get(campo) != b.get(campo):
            dif.append(f"{campo}: A={a.get(campo)!r} B={b.get(campo)!r}")
    for c in CONCEPTOS:
        x, y = normalizado(a, c), normalizado(b, c)
        if (x is None) != (y is None):
            dif.append(f"{c}: solo lo registra {'A' if x is not None else 'B'}")
        elif x is not None:
            tolerancia = 0.01 if c in POR_ACCION else 0.5 * min(FACTOR.get(a.get("unidades"), 1), FACTOR.get(b.get("unidades"), 1))
            if not abs(x - y) <= max(tolerancia, 1e-9 * abs(x)):
                dif.append(f"{c}: A={_cifras(a)[c]} B={_cifras(b)[c]}")
    return dif


def cifra_en_texto(valor: float | None, compacto: str) -> bool:
    """¿Aparece la cifra (sin signo: los negativos van entre paréntesis) en el texto?"""
    v = _num(valor)
    return v is not None and dividendos._cifra_en_texto(abs(v), compacto)


def verificar_cifras(r: dict, texto: str) -> list[str]:
    """Cifras que no aparecen en el texto de las páginas enviadas (protege contra valores inventados
    o calculados). Sin texto extraíble (páginas escaneadas) no se verifica."""
    compacto = dividendos._compacto(texto)
    if len(compacto) < 200:
        return []
    return [f"{c}: la cifra {v} no aparece en el texto de las páginas" for c, v in _cifras(r).items()
            if v is not None and not cifra_en_texto(v, compacto)]


def juzgar(a: dict, b: dict, texto: str) -> tuple[list[str], list[str]]:
    """(diferencias, avisos). Una cifra en la que coinciden pero que no está en el texto cuenta como
    diferencia: obliga a una revisión y, si sigue, deja el documento en disputa."""
    dif = comparar(a, b)
    if dif:
        return dif, []
    return verificar_cifras(a, texto), []


def controles(r: dict, emisor: dict, cfg: dict) -> tuple[dict[str, str], list[str]]:
    """(motivo de cuarentena por concepto, avisos) de un registro con consenso."""
    motivos: dict[str, str] = {}
    avisos: list[str] = []
    v = {c: normalizado(r, c) for c in CONCEPTOS}
    presentes = [c for c in CONCEPTOS if v[c] is not None]

    def marcar(conceptos, motivo):
        for c in conceptos:
            if v.get(c) is not None:
                motivos.setdefault(c, motivo)

    if not r.get("es_consolidado") and not emisor.get("acepta_separados"):
        marcar(presentes, "no_consolidado")
    corte = _fecha(r.get("fecha_corte"))
    if corte is None or (corte.month, corte.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
        marcar(presentes, "corte_invalido")
    if (r.get("moneda") or "") != emisor.get("moneda", "COP"):
        marcar(presentes, "moneda_inesperada")
    if r.get("unidades") not in FACTOR:
        marcar(presentes, "unidades_invalidas")
    elif r.get("unidades") not in (emisor.get("unidades_esperadas") or [r.get("unidades")]):
        avisos.append(f"unidades {r.get('unidades')} distintas de las esperadas {emisor.get('unidades_esperadas')}")
    if corte is not None and r.get("meses_acumulados") != corte.month:
        marcar(RESULTADOS, "resultados_no_acumulados")
    a, p, e = v["activos"], v["pasivos"], v["patrimonio_total"]
    if None not in (a, p, e) and abs(a - (p + e)) > float(cfg.get("tolerancia_balance", 0.01)) * abs(a):
        marcar(["activos", "pasivos", "patrimonio_total"], "control_balance")
    ec = v["patrimonio_controladora"]
    if ec is not None and e is not None and ec > e + float(cfg.get("tolerancia_controladora", 0.005)) * abs(e):
        marcar(["patrimonio_controladora"], "control_controladora")
    for c in ("activos", "pasivos", "efectivo", "ingresos_consolidados", *sorted(DEUDA)):
        if v[c] is not None and (v[c] < 0 or (c == "activos" and v[c] == 0)):
            marcar([c], "control_signo")
    if v["efectivo"] is not None and a is not None and v["efectivo"] > a:
        marcar(["efectivo"], "control_signo")
    uc, upa = v["utilidad_controladora"], v["upa_basica"]
    if uc is not None and upa is not None and uc * upa < 0:
        marcar(["upa_basica"], "control_signo")
    return motivos, avisos


def _fecha(texto) -> date | None:
    if isinstance(texto, date):
        return texto
    try:
        return date.fromisoformat(str(texto)[:10]) if texto else None
    except ValueError:
        return None


@dataclass
class ResultadoEE:
    estado: str          # consenso | en_disputa | sin_paginas | sin_texto | error
    final: dict | None
    a: dict | None
    b: dict | None
    rondas: int
    paginas: list[int]
    diferencias: list[str]
    alertas: list[str]


def extraer_documento(pdf: bytes, contexto: str, agente_a, agente_b, cfg: dict) -> ResultadoEE:
    """Ubica las páginas, envía solo esas a los dos agentes y juzga el consenso."""
    textos = textos_paginas(pdf)
    paginas = localizar_paginas(textos, cfg) if textos else []
    if not paginas:
        sin_texto = len(dividendos._compacto("".join(textos))) < 200
        n = int(cfg.get("limites", {}).get("paginas_si_no_hay_texto", 0))
        if not (sin_texto and n > 0 and textos):
            return ResultadoEE("sin_texto" if sin_texto else "sin_paginas", None, None, None, 0, [], [], [])
        paginas = list(range(min(n, len(textos))))
    texto = "\n".join(textos[i] for i in paginas)
    recorte = recortar(pdf, paginas)
    contexto = contexto + "\nPáginas enviadas (del documento original): " + ", ".join(str(i + 1) for i in paginas)
    a, b, rondas = dividendos.lectura_cruzada(recorte, contexto, agente_a, agente_b,
                                               lambda x, y: juzgar(x, y, texto)[0], int(cfg.get("rondas_maximas", 2)))
    dif, avisos = juzgar(a, b, texto)
    if dif:
        return ResultadoEE("en_disputa", None, a, b, rondas, paginas, dif, avisos)
    return ResultadoEE("consenso", a, a, b, rondas, paginas, [], avisos)


# --------------------------------------------------------------------------- filas
ESQUEMA_SILVER = {
    "emisor_id": pl.Utf8, "fecha_corte": pl.Date, "concepto": pl.Utf8, "valor": pl.Float64, "unidad": pl.Utf8,
    "valor_documento": pl.Float64, "unidades_documento": pl.Utf8, "archivo": pl.Utf8, "pagina": pl.Int64,
    "fecha_publicacion": pl.Date, "origen_fecha_publicacion": pl.Utf8, "fecha_descubierto": pl.Date,
    "estado": pl.Utf8, "motivo": pl.Utf8, "documento_id": pl.Utf8, "huella_pdf": pl.Utf8, "fuente_id": pl.Utf8,
    "ingest_ts": pl.Datetime("us", "UTC"),
}
ESQUEMA_EXTRACCION = {
    "documento_id": pl.Utf8, "emisor_id": pl.Utf8, "fecha_referencia": pl.Date, "archivos": pl.Utf8,
    "huella_pdf": pl.Utf8, "paginas": pl.Utf8, "paginas_total": pl.Int64, "estado": pl.Utf8, "rondas": pl.Int64,
    "diferencias": pl.Utf8, "alertas": pl.Utf8, "json_a": pl.Utf8, "json_b": pl.Utf8, "json_final": pl.Utf8,
    "modelo_a": pl.Utf8, "modelo_b": pl.Utf8, "procesado_ts": pl.Datetime("us", "UTC"),
}


def unidad_de(moneda: str, concepto: str) -> str:
    base = "usd" if moneda == "USD" else "pesos"
    return f"{base}_por_accion" if concepto in POR_ACCION else base


def version_de(reg: dict) -> int:
    """Versión de la lectura con que se leyó un documento (las anteriores a la 2 no la guardaban)."""
    for k in ("json_a", "json_final", "json_b"):
        if reg.get(k):
            try:
                return int(json.loads(reg[k]).get("version_lectura") or 1)
            except (ValueError, AttributeError):
                return 1
    return 1


# --------------------------------------------------------------------------- disputas
# Cuando los dos agentes no se ponen de acuerdo, cada concepto se decide por separado:
#   1. donde coinciden, vale (antes se iba todo el documento a cuarentena por una sola cifra);
#   2. activos, pasivos y patrimonio total: si solo una de las dos propuestas cumple activos = pasivos +
#      patrimonio (± tolerancia_balance) con lo que sí coincide, vale esa ("resuelto");
#   3. lo que siga pendiente lo lee un árbitro (otro modelo, por omisión Claude Fable 5.2, con las páginas
#      de los estados y las que traen las cifras en disputa): vale la propuesta con la que coincide, si
#      su cifra está en el texto ("arbitrado");
#   4. lo demás queda en invest-data/revision/estados_compania.csv para decidirlo a mano mirando el PDF
#      (columna revision: a, b, corregido con valor_revisado, o rechazado) y entra en la próxima corrida
#      sin volver a llamar a la API ("revisado").
RESOLUCION_VERSION = 3  # 3: el cero del árbitro vale si coincide con una lectura
ESTADOS_ACEPTADOS = {"consenso", "resuelto", "arbitrado", "revisado"}
ARCHIVO_REVISION = "estados_compania.csv"
COLUMNAS_REVISION = ["documento_id", "emisor_id", "nombre", "fecha_corte", "concepto", "unidades_a", "valor_a",
                     "pagina_a", "unidades_b", "valor_b", "pagina_b", "unidades_arbitro", "valor_arbitro",
                     "pagina_arbitro", "modelo_arbitro", "archivo", "paginas_enviadas", "diferencia",
                     "revision", "valor_revisado", "notas_revision"]
ENCABEZADO = ("es_consolidado", "fecha_corte", "moneda", "unidades", "meses_acumulados")


def _pagina_original(r: dict | None, concepto: str, paginas: list[int]) -> int | None:
    pag = (((r or {}).get("cifras") or {}).get(concepto) or {}).get("pagina")
    return paginas[pag - 1] + 1 if isinstance(pag, int) and 1 <= pag <= len(paginas) else None


def _iguales(a: dict, b: dict, c: str) -> bool:
    x, y = normalizado(a, c), normalizado(b, c)
    if x is None or y is None:
        return x is None and y is None
    tolerancia = 0.01 if c in POR_ACCION else 0.5 * min(FACTOR.get(a.get("unidades"), 1), FACTOR.get(b.get("unidades"), 1))
    return abs(x - y) <= max(tolerancia, 1e-9 * abs(x))


def resolver_disputa(a: dict | None, b: dict | None, cfg: dict) -> tuple[dict[str, tuple[str, str]], list[str], list[str]]:
    """({concepto: (agente "a"/"b", "acuerdo"/"balance")}, conceptos pendientes, diferencias de encabezado).
    Con diferencias de encabezado (consolidado, corte, moneda, unidades, meses) no se decide nada solo."""
    if not a or not b:
        return {}, [c for c in CONCEPTOS if _cifras(a or b or {})[c] is not None], ["falta una de las dos lecturas"]
    encabezado = [f"{k}: A={a.get(k)!r} B={b.get(k)!r}" for k in ENCABEZADO if a.get(k) != b.get(k)]
    presentes = [c for c in CONCEPTOS if _cifras(a)[c] is not None or _cifras(b)[c] is not None]
    if encabezado:
        return {}, presentes, encabezado
    decidido = {c: ("a", "acuerdo") for c in presentes if _iguales(a, b, c)}
    pendientes = [c for c in presentes if c not in decidido]
    tol = float(cfg.get("tolerancia_balance", 0.01))
    trio = ("activos", "pasivos", "patrimonio_total")
    for c in [x for x in trio if x in pendientes]:
        otros = [x for x in trio if x != c]
        if any(x not in decidido for x in otros):
            continue
        v = {x: normalizado(a if decidido[x][0] == "a" else b, x) for x in otros}
        if None in v.values():
            continue
        cumple = []
        for agente, r in (("a", a), ("b", b)):
            cand = normalizado(r, c)
            if cand is None:
                continue
            act = cand if c == "activos" else v["activos"]
            suma = (cand if c == "pasivos" else v["pasivos"]) + (cand if c == "patrimonio_total" else v["patrimonio_total"])
            if abs(act - suma) <= tol * abs(act):
                cumple.append(agente)
        if len(cumple) == 1:
            decidido[c] = (cumple[0], "balance")
            pendientes.remove(c)
    return decidido, pendientes, []


def paginas_arbitraje(textos: list[str], cfg: dict, a: dict | None, b: dict | None, conceptos: list[str]) -> list[int]:
    """Páginas para el árbitro: las de los estados principales y las que traen alguna de las cifras en
    disputa (Paz del Río jun-2026: a los dos lectores solo les llegó una página de notas)."""
    base = localizar_paginas(textos, cfg)
    con_cifra = []
    for i, t in enumerate(textos):
        compacto = dividendos._compacto(t)
        if any(r and _cifras(r)[c] is not None and cifra_en_texto(_cifras(r)[c], compacto)
               for r in (a, b) for c in conceptos):
            con_cifra.append(i)
    tope = int(cfg.get("limites", {}).get("max_paginas_arbitraje", 12))
    elegidas = list(dict.fromkeys(con_cifra + base))[:tope]
    return sorted(elegidas)


def texto_arbitraje(contexto: str, a: dict | None, b: dict | None, pendientes: list[str], encabezado: list[str]) -> str:
    lineas = [contexto, "", "Los dos lectores no coinciden en:"]
    lineas += [f"- {d}" for d in encabezado]
    for c in pendientes:
        va, vb = (_cifras(a)[c] if a else None), (_cifras(b)[c] if b else None)
        lineas.append(f"- {c}: lector A = {va} ({(a or {}).get('unidades')}), lector B = {vb} ({(b or {}).get('unidades')})")
    lineas += ["", "Lee el documento y registra todas las cifras del periodo del informe con su página y rótulo."]
    return "\n".join(lineas)


def arbitrar(pdf: bytes, contexto: str, a: dict | None, b: dict | None, cliente, modelos: list[str], cfg: dict) -> dict:
    """Tercera lectura, con otro modelo, de un documento en disputa. Para cada cifra pendiente vale la
    propuesta (A o B) con la que coincide el árbitro, si además su cifra aparece en el texto de las
    páginas. Si el árbitro no coincide con ninguno, queda para revisión a mano con la imagen."""
    _, pendientes, encabezado = resolver_disputa(a, b, cfg)
    textos = textos_paginas(pdf)
    paginas = paginas_arbitraje(textos, cfg, a, b, pendientes) if textos else []
    if not paginas:
        return {"estado": "sin_paginas", "modelo": None, "paginas": [], "encabezado": None, "decisiones": {}}
    texto = "\n".join(textos[i] for i in paginas)
    recorte = recortar(pdf, paginas)
    pedido = texto_arbitraje(contexto + "\nPáginas enviadas (del documento original): "
                             + ", ".join(str(i + 1) for i in paginas), a, b, pendientes, encabezado)
    r, modelo, ultimo_error = None, None, None
    for m in modelos:  # el primero que responda (Fable 5.2; si la cuenta no lo tiene, el respaldo)
        try:
            r = cliente.registrar(m, INSTRUCCIONES["arbitro"], recorte, pedido)
            modelo = m
            break
        except Exception as e:  # noqa: BLE001 - modelo no disponible para la cuenta
            ultimo_error = e
            if "model" not in str(e).lower():
                raise
    if r is None:
        raise ultimo_error or RuntimeError("sin modelo para el arbitraje")
    compacto = dividendos._compacto(texto)
    verificable = len(compacto) >= 200
    decisiones = {}
    for c in pendientes:
        vr = _cifras(r)[c]
        decisiones[c] = None
        # Un cero no se puede buscar en el texto (los estados lo imprimen como «-»): basta que coincida
        if vr is None or (vr != 0 and verificable and not cifra_en_texto(vr, compacto)):
            continue
        decisiones[c] = _lado_que_coincide(r, a, b, c)
    lado = None
    if encabezado:
        for ag, x in (("a", a), ("b", b)):
            if x and all(r.get(k) == x.get(k) for k in ENCABEZADO):
                lado = ag
    return {"estado": "ok", "modelo": modelo, "paginas": paginas, "encabezado": lado, "decisiones": decisiones,
            "registro": r, "version": VERSION_LECTURA}


def _lado_que_coincide(r: dict, a: dict | None, b: dict | None, c: str) -> str | None:
    """'a' o 'b': la lectura con la que coincide el árbitro en el concepto c."""
    for ag, x in (("a", a), ("b", b)):
        if x and _cifras(x)[c] is not None and _iguales(r, x, c):
            return ag
    return None


def ruta_revision() -> Path:
    return data_dir() / "revision" / ARCHIVO_REVISION


def leer_revision() -> dict[tuple[str, str], dict]:
    """{(documento_id, concepto): fila con decisión} del archivo de revisión (solo las decididas)."""
    ruta = ruta_revision()
    if not ruta.exists():
        return {}
    with open(ruta, encoding="utf-8-sig", errors="replace", newline="") as f:
        filas = list(csv.DictReader(f, delimiter=";"))
    salida = {}
    for r in filas:
        decision = (r.get("revision") or "").strip().lower()
        if decision in ("a", "b", "corregido", "rechazado"):
            salida[(r["documento_id"], r["concepto"])] = {**r, "revision": decision}
    return salida


def _a_numero(texto) -> float | None:
    t = str(texto or "").strip().replace("$", "").replace(" ", "")
    if not t:
        return None
    if "," in t:  # 1.234.567,89 (como lo escribe Excel en español)
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def huella_revision(documento_id: str, revision: dict) -> str:
    """Huella de las decisiones de un documento: si cambian, el documento se vuelve a incorporar."""
    partes = sorted(f"{c}={d['revision']}:{d.get('valor_revisado', '')}" for (doc, c), d in revision.items()
                    if doc == documento_id)
    return hashlib.sha1("|".join(partes).encode()).hexdigest()[:8] if partes else "-"


def _con_arbitraje(a, b, cfg, arbitraje: dict | None):
    """Lo decidido solo (acuerdo, balance) más lo que decidió el árbitro: (elegido, pendientes, encabezado,
    lado elegido por el árbitro para el encabezado)."""
    decidido, pendientes, encabezado = resolver_disputa(a, b, cfg)
    elegido = {c: (ag, "resuelto") for c, (ag, _) in decidido.items()}
    lado = None
    if arbitraje and arbitraje.get("estado") == "ok":
        lado = arbitraje.get("encabezado")
        if encabezado and not lado:
            return elegido, pendientes, encabezado, None
        registro = arbitraje.get("registro") or {}
        for c in list(pendientes):
            ag = (arbitraje.get("decisiones") or {}).get(c)
            if not ag and registro and _cifras(registro)[c] == 0:
                ag = _lado_que_coincide(registro, a, b, c)  # arbitrajes guardados antes de aceptar ceros
            if ag and (not encabezado or ag == lado):
                elegido[c] = (ag, "arbitrado")
                pendientes.remove(c)
    return elegido, pendientes, encabezado, lado


def filas_revision(doc: dict, log: dict, emisor: dict, cfg: dict, arbitraje: dict | None = None) -> list[dict]:
    """Filas para el archivo de revisión de un documento en disputa: lo que no se pudo decidir solo."""
    if log.get("estado") != "en_disputa":
        return []
    a = json.loads(log["json_a"]) if log.get("json_a") else None
    b = json.loads(log["json_b"]) if log.get("json_b") else None
    _, pendientes, encabezado, lado = _con_arbitraje(a, b, cfg, arbitraje)
    if encabezado and lado:
        encabezado = []  # el árbitro eligió la lectura: lo pendiente es por concepto
    paginas = [int(p) for p in (log.get("paginas") or "").split(",") if p]
    arb = (arbitraje or {}).get("registro") or {}
    pag_arb = (arbitraje or {}).get("paginas") or []
    extra = lambda c: {"unidades_arbitro": arb.get("unidades", ""),  # noqa: E731
                       "valor_arbitro": "" if not arb or _cifras(arb)[c] is None else _cifras(arb)[c],
                       "pagina_arbitro": (_pagina_original(arb, c, pag_arb) or "") if arb else "",
                       "modelo_arbitro": (arbitraje or {}).get("modelo") or ""}
    base = {"documento_id": doc["documento_id"], "emisor_id": doc["emisor_id"], "nombre": emisor.get("nombre", ""),
            "fecha_corte": str(doc.get("corte") or (a or b or {}).get("fecha_corte") or ""),
            "archivo": doc.get("archivo", ""), "paginas_enviadas": ",".join(str(i + 1) for i in paginas),
            "revision": "", "valor_revisado": "", "notas_revision": ""}
    if encabezado:
        return [{**base, "concepto": "documento", "unidades_a": (a or {}).get("unidades", ""), "valor_a": "",
                 "pagina_a": "", "unidades_b": (b or {}).get("unidades", ""), "valor_b": "", "pagina_b": "",
                 "unidades_arbitro": arb.get("unidades", ""), "valor_arbitro": "", "pagina_arbitro": "",
                 "modelo_arbitro": (arbitraje or {}).get("modelo") or "",
                 "diferencia": "; ".join(encabezado) + " (a o b elige la lectura completa)"}]
    salida = []
    for c in pendientes:
        va, vb = _cifras(a)[c], _cifras(b)[c]
        salida.append({**base, **extra(c), "concepto": c, "unidades_a": a.get("unidades", ""), "valor_a": "" if va is None else va,
                       "pagina_a": _pagina_original(a, c, paginas) or "", "unidades_b": b.get("unidades", ""),
                       "valor_b": "" if vb is None else vb, "pagina_b": _pagina_original(b, c, paginas) or "",
                       "diferencia": "solo lo registra A" if vb is None else "solo lo registra B" if va is None else "cifras distintas"})
    return salida


def exportar_revision(pendientes: list[dict], revisados: set[str] | None = None) -> Path | None:
    """Reescribe el archivo de revisión: las filas nuevas sin decidir y las ya decididas, tal como están.
    De los documentos de revisados (los que se juzgaron en esta corrida) solo quedan las cifras que siguen
    en disputa: si se releyó y hubo consenso, o el árbitro ya lo resolvió, la decisión vieja no aplica."""
    ruta = ruta_revision()
    previas = []
    if ruta.exists():
        with open(ruta, encoding="utf-8-sig", errors="replace", newline="") as f:
            previas = list(csv.DictReader(f, delimiter=";"))
    if revisados:
        vigentes = {(r["documento_id"], r["concepto"]) for r in pendientes}
        previas = [r for r in previas if r.get("documento_id") not in revisados
                   or (r.get("documento_id"), r.get("concepto")) in vigentes]
    decididas = {(r["documento_id"], r["concepto"]): r for r in previas if (r.get("revision") or "").strip()}
    filas = list(decididas.values()) + [r for r in pendientes if (r["documento_id"], r["concepto"]) not in decididas]
    if not filas and not ruta.exists():
        return None
    ruta.parent.mkdir(parents=True, exist_ok=True)
    # Punto y coma y BOM: Excel en español lo abre en columnas y con tildes
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS_REVISION, delimiter=";", extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(sorted(filas, key=lambda r: (r["emisor_id"], r["fecha_corte"], r["concepto"])))
    return ruta


def filas_documento(doc: dict, log: dict, emisor: dict, cfg: dict, revision: dict | None = None,
                    arbitraje: dict | None = None) -> list[dict]:
    """Filas de silver (motivo vacío) y de cuarentena (con motivo) de un documento ya leído."""
    comunes = {k: doc.get(k) for k in ("emisor_id", "archivo", "fecha_publicacion", "origen_fecha_publicacion",
                                       "fecha_descubierto", "documento_id", "huella_pdf")}
    comunes.update(fuente_id=FUENTE_ID, ingest_ts=datetime.now(timezone.utc))
    paginas = [int(p) for p in (log.get("paginas") or "").split(",") if p]
    estado = log["estado"]
    a = json.loads(log["json_a"]) if log.get("json_a") else None
    b = json.loads(log["json_b"]) if log.get("json_b") else None
    if estado not in ("consenso", "en_disputa"):
        return [{**comunes, "fecha_corte": doc.get("corte") or doc.get("fecha_descubierto"), "concepto": "documento",
                 "valor": None, "unidad": None, "valor_documento": None, "unidades_documento": None,
                 "pagina": None, "estado": estado, "motivo": estado}]
    if estado == "en_disputa":
        return _filas_disputa(doc, a, b, paginas, comunes, emisor, cfg, revision or {}, arbitraje)
    r = a
    corte = _fecha(r.get("fecha_corte"))
    motivos, _ = controles(r, emisor, cfg)
    salida = []
    for c in CONCEPTOS:
        bruto = _cifras(r)[c]
        if bruto is None:
            continue
        pag = ((r.get("cifras") or {}).get(c) or {}).get("pagina")
        original = paginas[pag - 1] + 1 if isinstance(pag, int) and 1 <= pag <= len(paginas) else None
        salida.append({**comunes, "fecha_corte": corte or doc.get("fecha_descubierto"), "concepto": c,
                       "valor": normalizado(r, c), "unidad": unidad_de(r.get("moneda") or emisor.get("moneda", "COP"), c),
                       "valor_documento": bruto, "unidades_documento": r.get("unidades"), "pagina": original,
                       "estado": estado, "motivo": motivos.get(c)})
    return salida


def _filas_disputa(doc, a, b, paginas, comunes, emisor, cfg, revision, arbitraje=None) -> list[dict]:
    """Un documento en disputa, concepto por concepto (ver «disputas» arriba)."""
    elegido, pendientes, encabezado, lado = _con_arbitraje(a, b, cfg, arbitraje)
    rechazados, corregidos = set(), {}
    doc_rev = revision.get((doc["documento_id"], "documento"))
    if encabezado and lado and not doc_rev:
        doc_rev = {"revision": lado, "arbitro": True}
    if encabezado and doc_rev and doc_rev["revision"] in ("a", "b"):
        # La persona eligió una de las dos lecturas completas
        r = a if doc_rev["revision"] == "a" else b
        if not doc_rev.get("arbitro"):
            elegido = {c: (doc_rev["revision"], "revisado") for c in CONCEPTOS if _cifras(r)[c] is not None}
            pendientes = []
    for c in list(pendientes):
        d = revision.get((doc["documento_id"], c))
        if not d:
            continue
        pendientes.remove(c)
        if d["revision"] in ("a", "b"):
            elegido[c] = (d["revision"], "revisado")
        elif d["revision"] == "corregido" and _a_numero(d.get("valor_revisado")) is not None:
            corregidos[c] = _a_numero(d["valor_revisado"])  # en las unidades de la lectura A
            elegido[c] = ("a", "revisado")
        else:
            rechazados.add(c)
    # Registro combinado en pesos para los controles (balance, signos, corte, moneda)
    base = a or b
    combinado = {k: base.get(k) for k in ("es_consolidado", "fecha_corte", "moneda", "meses_acumulados")}
    if encabezado and doc_rev and doc_rev["revision"] in ("a", "b"):
        combinado = {k: (a if doc_rev["revision"] == "a" else b).get(k) for k in combinado}
    combinado["unidades"] = "pesos"
    combinado["cifras"] = {}
    for c, (ag, _) in elegido.items():
        r = a if ag == "a" else b
        v = corregidos.get(c, _cifras(r)[c])
        if v is not None:
            combinado["cifras"][c] = {"valor": v if c in POR_ACCION else v * FACTOR.get(r.get("unidades"), float("nan"))}
    motivos, _ = controles(combinado, emisor, cfg) if elegido else ({}, [])
    corte = _fecha(combinado.get("fecha_corte"))
    if encabezado and not (doc_rev and doc_rev["revision"] in ("a", "b")):
        corte = doc.get("corte") or corte
    salida = []
    for c in CONCEPTOS:
        if c in elegido:
            ag, estado = elegido[c]
            r = a if ag == "a" else b
            bruto = corregidos.get(c, _cifras(r)[c])
            salida.append({**comunes, "fecha_corte": corte or doc.get("fecha_descubierto"), "concepto": c,
                           "valor": normalizado({**r, "cifras": {c: {"valor": bruto}}}, c),
                           "unidad": unidad_de(r.get("moneda") or emisor.get("moneda", "COP"), c),
                           "valor_documento": bruto, "unidades_documento": r.get("unidades"),
                           "pagina": _pagina_original(r, c, paginas), "estado": estado, "motivo": motivos.get(c)})
        elif c in pendientes or c in rechazados:
            r = a if (a and _cifras(a)[c] is not None) else b
            salida.append({**comunes, "fecha_corte": corte or doc.get("fecha_descubierto"), "concepto": c,
                           "valor": normalizado(r, c), "unidad": unidad_de(r.get("moneda") or emisor.get("moneda", "COP"), c),
                           "valor_documento": _cifras(r)[c], "unidades_documento": r.get("unidades"),
                           "pagina": _pagina_original(r, c, paginas), "estado": "en_disputa",
                           "motivo": "rechazado_en_revision" if c in rechazados else "sin_consenso"})
    return salida


# --------------------------------------------------------------------------- conector
def _huella(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


class ConectorEstadosCompania(Conector):
    fuente_id = FUENTE_ID
    tabla = TABLA
    claves = ("emisor_id", "fecha_corte", "concepto")
    columna_fecha = "fecha_corte"

    def __init__(self, cliente=None, hoy: date | None = None) -> None:
        super().__init__()
        self.cliente = cliente
        self.hoy = hoy
        self.cfg = config()

    # ----------------------------------------------------------------- descubrir y bajar
    def _descubrir(self, emisor: dict, hoy: date) -> list[dict]:
        lim = self.cfg.get("limites", {})
        lista: list[dict] = []
        for pagina in emisor.get("paginas_indice") or []:
            try:
                contenido = self.bajar(pagina["url"])
            except http.ErrorHTTP as e:
                self._fallo(emisor, e)
                if isinstance(e, http.AccesoDenegado):
                    return []
                continue
            memoria = self.estado_nuevo.setdefault("indices", {})
            if contenido is None:  # sin cambios: se usan los enlaces que dio la última vez
                lista += [{**c, "corte": _fecha(c.get("corte"))} for c in memoria.get(pagina["url"], [])]
                continue
            if es_bloqueo(contenido):
                self._bloqueo(emisor["emisor_id"], "página índice con desafío contra robots")
                return []
            encontrados = candidatos(emisor, enlaces(contenido.decode("utf-8", errors="replace"), pagina["url"]))
            memoria[pagina["url"]] = [{**c, "corte": c["corte"].isoformat() if c["corte"] else None} for c in encontrados]
            if not encontrados:
                self.conteo["indices_sin_enlaces"] += 1
            lista += encontrados
        lista += urls_plantilla(emisor, hoy, int(lim.get("trimestres_plantilla", 4)),
                                int(lim.get("dias_tras_corte_plantilla", 25)))
        # Direcciones verificadas a mano cuando el sitio no tiene una página índice legible ni una plantilla
        lista += [{"url": d["url"], "texto": "", "prioridad": 0, "corte": _fecha(str(d.get("corte")))}
                  for d in emisor.get("directas") or []]
        return lista

    def _fallo(self, emisor: dict, error: Exception) -> None:
        self.conteo["fallidos"] += 1
        if isinstance(error, http.AccesoDenegado):
            self._bloqueo(emisor["emisor_id"], "401/403")

    def _bloqueo(self, emisor_id: str, motivo: str) -> None:
        self.bloqueados.add(emisor_id)
        self.estado_nuevo.setdefault("bloqueos", {})[emisor_id] = f"{self._hoy().isoformat()} {motivo}"

    def _hoy(self) -> date:
        return self.hoy or date.today()

    def _bajar_pdf(self, emisor: dict, cand: dict) -> None:
        docs = self.estado_nuevo.setdefault("documentos", {})
        url = cand["url"]
        no_encontrados = self.estado_nuevo.setdefault("no_encontrados", {})
        lim = self.cfg.get("limites", {})
        hoy = self._hoy()
        if url in docs and not self.forzado:
            return
        visto = _fecha(no_encontrados.get(url))
        if visto and (hoy - visto).days < int(lim.get("reintentar_404_dias", 7)) and not self.forzado:
            return
        if self.conteo["bajados"] >= int(lim.get("max_descargas_por_corrida", 20)):
            self.conteo["descargas_pospuestas"] += 1
            return
        try:
            contenido = self.bajar(url, reintentos=2, timeout=120)
        except http.NoEncontrado:
            no_encontrados[url] = hoy.isoformat()
            return
        except http.ErrorHTTP as e:
            self._fallo(emisor, e)
            return
        if contenido is None:  # corrida forzada y el servidor dice que no cambió
            return
        if not es_pdf(contenido):
            if es_bloqueo(contenido):
                self._bloqueo(emisor["emisor_id"], "el PDF responde con un desafío contra robots")
            else:
                no_encontrados[url] = hoy.isoformat()
            return
        if len(contenido) > float(lim.get("tamano_maximo_mb", 40)) * 1024 * 1024:
            self.conteo["demasiado_grandes"] += 1
            return
        corte = cand.get("corte")
        relativa = f"{CARPETA_ANEXOS}/{emisor['emisor_id']}/{corte.isoformat() if corte else 'sin_corte'}_{_nombre_archivo(url)}"
        storage.guardar_anexo(relativa, contenido)
        validadores = self.estado_nuevo.get("http", {}).get(url, {})
        publicada, origen = fecha_publicacion(url, validadores.get("last_modified"))
        previo = docs.get(url) or {}
        huella = _huella(contenido)
        descubierto = previo.get("fecha_descubierto") if previo.get("huella") == huella else None
        docs[url] = {"emisor_id": emisor["emisor_id"], "archivo": f"anexos/{relativa}", "huella": huella,
                     "corte": corte.isoformat() if corte else None, "prioridad": cand.get("prioridad", 0),
                     "fecha_publicacion": publicada.isoformat() if publicada else None, "origen_fecha_publicacion": origen,
                     "fecha_descubierto": descubierto or hoy.isoformat()}
        no_encontrados.pop(url, None)
        self.conteo["bajados"] += 1

    def _manuales(self, emisor: dict) -> None:
        carpeta = data_dir() / CARPETA_MANUAL / emisor["emisor_id"]
        if not carpeta.exists():
            return
        docs = self.estado_nuevo.setdefault("documentos", {})
        for ruta in sorted(carpeta.glob("*.pdf")) + sorted(carpeta.glob("*.PDF")):
            clave = f"manual:{emisor['emisor_id']}/{ruta.name}"
            contenido = ruta.read_bytes()
            huella = _huella(contenido)
            if clave in docs and docs[clave]["huella"] == huella:
                continue
            if not es_pdf(contenido):
                continue
            corte = corte_probable(ruta.name)
            docs[clave] = {"emisor_id": emisor["emisor_id"], "archivo": ruta.relative_to(data_dir()).as_posix(),
                           "huella": huella, "corte": corte.isoformat() if corte else None, "prioridad": -1,
                           "fecha_publicacion": None, "origen_fecha_publicacion": "manual",
                           "fecha_descubierto": self._hoy().isoformat()}
            self.conteo["manuales"] += 1

    def _eventos_sfc(self) -> list[dict]:
        """Eventos de información relevante con estados financieros y anexo guardado (una vez por corrida)."""
        if getattr(self, "_cache_sfc", None) is None:
            from invest.ingest import sfc_relevante
            ev = storage.leer_tabla("silver", "fact_evento_sfc")
            filas: list[dict] = []
            if ev is not None and not ev.is_empty():
                emisores = eventos.tabla_emisores(storage.leer_tabla("silver", "dim_especie_sfc"))
                alias = eventos.alias_manuales()
                for r in ev.filter(pl.col("anexo_archivo").is_not_null()).iter_rows(named=True):
                    emisor = eventos.emisor_de(r["entidad"], emisores, alias)
                    if emisor and sfc_relevante.es_informe_financiero(r["tema"], r["resumen"]):
                        filas.append({**r, "emisor_id": f"CO-{emisor.upper()}",
                                      "corte": sfc_relevante.corte_de_resumen(r["resumen"], r["fecha"])})
            self._cache_sfc = filas
        return self._cache_sfc

    def _desde_sfc(self, emisor: dict) -> None:
        """Estados financieros que el emisor transmitió como información relevante a la SFC (los baja
        ``sfc_informacion_relevante``). Si el anexo es un ZIP se guarda aparte el PDF de los estados."""
        docs = self.estado_nuevo.setdefault("documentos", {})
        # Lo que ya no cuenta como estados financieros (p. ej. el informe de sostenibilidad de Paz del Río,
        # 26-nov-2026) sale de la lista
        vigentes = {f"sfc:{ev['evento_id']}" for ev in self._eventos_sfc() if ev["emisor_id"] == emisor["emisor_id"]}
        for clave in [k for k, d in docs.items() if k.startswith("sfc:") and d["emisor_id"] == emisor["emisor_id"]
                      and k not in vigentes]:
            del docs[clave]
        for ev in self._eventos_sfc():
            if ev["emisor_id"] != emisor["emisor_id"]:
                continue
            clave = f"sfc:{ev['evento_id']}"
            ruta = data_dir() / "anexos" / ev["anexo_archivo"]
            if not ruta.exists():
                continue
            contenido = ruta.read_bytes()
            huella = _huella(contenido)
            if clave in docs and docs[clave].get("huella_origen", docs[clave]["huella"]) == huella:
                continue
            archivo = f"anexos/{ev['anexo_archivo']}"
            if not es_pdf(contenido):
                pdf = pdf_de_zip(contenido)
                if pdf is None:
                    continue
                corte_txt = ev["corte"].isoformat() if ev["corte"] else "sin_corte"
                relativa = f"{CARPETA_ANEXOS}/{emisor['emisor_id']}/{corte_txt}_sfc_{ev['evento_id']}.pdf"
                storage.guardar_anexo(relativa, pdf)
                archivo = f"anexos/{relativa}"
            r = eventos.normal(ev["resumen"])
            # Prioridad (menor gana): lo del sitio de la compañía (0) antes que la SFC, y en la SFC el
            # consolidado antes que el separado
            prioridad = 2 if "SEPARADO" in r and "CONSOLIDADO" not in r else 1
            docs[clave] = {"emisor_id": emisor["emisor_id"], "archivo": archivo, "huella": _huella((data_dir() / archivo).read_bytes()),
                           "huella_origen": huella, "corte": ev["corte"].isoformat() if ev["corte"] else None,
                           "prioridad": prioridad, "fecha_publicacion": ev["fecha"].isoformat(),
                           "origen_fecha_publicacion": "sfc_informacion_relevante",
                           "fecha_descubierto": self._hoy().isoformat()}
            self.conteo["desde_sfc"] += 1

    # ----------------------------------------------------------------- documentos
    def documentos(self) -> list[dict]:
        """Agrupa los archivos guardados por emisor y corte: uno por corte (el de más prioridad, y
        entre iguales el más reciente) o todos unidos si el emisor tiene ``combinar_por_corte``."""
        emisores = emisores_config(self.cfg)
        grupos: dict[tuple, list[tuple[str, dict]]] = {}
        for clave, d in (self.estado_nuevo.get("documentos") or {}).items():
            if d["emisor_id"] not in emisores or not (data_dir() / d["archivo"]).exists():
                continue
            grupo = (d["emisor_id"], d.get("corte") or f"sin_corte:{clave}")
            grupos.setdefault(grupo, []).append((clave, d))
        salida = []
        for (emisor_id, corte), miembros in grupos.items():
            em = emisores[emisor_id]
            if em.get("combinar_por_corte"):
                if len(miembros) < int(em.get("archivos_por_corte", 1)):
                    continue
                elegidos = sorted(miembros)
            else:
                elegidos = [min(miembros, key=lambda m: (m[1].get("prioridad", 0), _neg(m[1].get("fecha_descubierto")), m[0]))]
            fechas = [_fecha(m[1].get("fecha_publicacion")) for m in elegidos]
            claves = [m[0] for m in elegidos]
            salida.append({
                "documento_id": hashlib.sha1("|".join(claves).encode()).hexdigest()[:16],
                "emisor_id": emisor_id, "claves": claves, "archivos": [m[1]["archivo"] for m in elegidos],
                "archivo": ";".join(m[1]["archivo"] for m in elegidos),
                "huella_pdf": hashlib.sha256("".join(m[1]["huella"] for m in elegidos).encode()).hexdigest(),
                "corte": _fecha(corte), "fecha_publicacion": max(fechas) if all(fechas) else None,
                "origen_fecha_publicacion": ",".join(sorted({m[1].get("origen_fecha_publicacion") or "" for m in elegidos})) or None,
                "fecha_descubierto": max(_fecha(m[1].get("fecha_descubierto")) for m in elegidos),
            })
        return sorted(salida, key=lambda d: (d["corte"] or date.min, d["emisor_id"]), reverse=True)

    def _agentes(self, cliente):
        ag = self.cfg.get("agentes") or dividendos.config()["agentes"]
        return (AgenteEE("a", ag["a"]["nombre"], ag["a"]["modelo"], cliente),
                AgenteEE("b", ag["b"]["nombre"], ag["b"]["modelo"], cliente))

    def _leer(self, pendientes: list[dict]) -> list[dict]:
        """Lee con los dos agentes hasta ``max_documentos_por_corrida`` documentos pendientes."""
        lim = self.cfg.get("limites", {})
        # INVEST_EE_MAX_DOCUMENTOS: tope de una corrida puntual (scripts/windows/recuperar.ps1 -Documentos N)
        maximo = int(os.environ.get("INVEST_EE_MAX_DOCUMENTOS") or lim.get("max_documentos_por_corrida", 6))
        if not pendientes:
            return []
        cliente = self.cliente
        if cliente is None and not os.environ.get("ANTHROPIC_API_KEY"):
            self.notas.append(f"lectura omitida: falta ANTHROPIC_API_KEY ({len(pendientes)} documentos pendientes)")
            return []
        if cliente is None:
            cliente = dividendos.ClienteLLM(int(self.cfg.get("max_tokens", 16000)), herramienta=HERRAMIENTA)
        agente_a, agente_b = self._agentes(cliente)
        emisores = emisores_config(self.cfg)
        filas = []
        for doc in pendientes[:maximo]:
            em = emisores[doc["emisor_id"]]
            base = {"documento_id": doc["documento_id"], "emisor_id": doc["emisor_id"],
                    "fecha_referencia": doc["corte"] or doc["fecha_descubierto"], "archivos": doc["archivo"],
                    "huella_pdf": doc["huella_pdf"], "modelo_a": agente_a.modelo, "modelo_b": agente_b.modelo,
                    "procesado_ts": datetime.now(timezone.utc)}
            try:
                pdf = unir_pdf([(data_dir() / a).read_bytes() for a in doc["archivos"]])
                contexto = (f"Emisor: {em['nombre']}\nMoneda en que reporta: {em.get('moneda', 'COP')}\n"
                            f"Corte probable según el nombre del archivo: {doc['corte'] or 'desconocido'}")
                r = extraer_documento(pdf, contexto, agente_a, agente_b, self.cfg)
                for j in (r.a, r.b, r.final):
                    if isinstance(j, dict):
                        j["version_lectura"] = VERSION_LECTURA
                total = numero_paginas(pdf)
                filas.append({**base, "estado": r.estado, "rondas": r.rondas, "paginas": ",".join(map(str, r.paginas)),
                              "paginas_total": total, "diferencias": "\n".join(r.diferencias), "alertas": "\n".join(r.alertas),
                              "json_a": json.dumps(r.a, ensure_ascii=False) if r.a else None,
                              "json_b": json.dumps(r.b, ensure_ascii=False) if r.b else None,
                              "json_final": json.dumps(r.final, ensure_ascii=False) if r.final else None})
                self.conteo[r.estado] = self.conteo.get(r.estado, 0) + 1
                self.conteo["paginas_enviadas"] += len(r.paginas)
            except Exception as e:  # el documento queda en error y se reintenta en la próxima corrida
                filas.append({**base, "estado": "error", "rondas": 0, "paginas": "", "paginas_total": None,
                              "diferencias": f"{type(e).__name__}: {e}", "alertas": "", "json_a": None,
                              "json_b": None, "json_final": None})
                self.conteo["error"] = self.conteo.get("error", 0) + 1
        if len(pendientes) > maximo:
            self.notas.append(f"{len(pendientes) - maximo} documentos quedan para la próxima corrida")
        return filas

    def _paginas_cambiaron(self, d: dict, leidos: dict) -> bool:
        reg = leidos.get((d["documento_id"], d["huella_pdf"]))
        if reg is None or reg["estado"] != "en_disputa":
            return False
        try:
            pdf = unir_pdf([(data_dir() / x).read_bytes() for x in d["archivos"]])
        except OSError:
            return False
        ahora = localizar_paginas(textos_paginas(pdf), self.cfg)
        antes = [int(p) for p in (reg.get("paginas") or "").split(",") if p]  # índices desde 0
        return bool(ahora) and sorted(ahora) != sorted(antes)

    def _modelos_arbitro(self) -> list[str]:
        ag = (self.cfg.get("agentes") or {}).get("arbitro") or {}
        return [m for m in (ag.get("modelo") or "claude-fable-5-2", ag.get("respaldo") or "claude-opus-5-5") if m]

    def _arbitrar(self, docs: list[dict], leidos: dict, arbitrajes: dict) -> None:
        """Tercera lectura de los documentos en disputa que aún tienen cifras sin decidir (una vez por PDF)."""
        emisores = emisores_config(self.cfg)
        lim = self.cfg.get("limites", {})
        maximo = int(os.environ.get("INVEST_EE_MAX_ARBITRAJES") or lim.get("max_arbitrajes_por_corrida", 10))
        candidatos = []
        for d in docs:
            reg = leidos.get((d["documento_id"], d["huella_pdf"]))
            if reg is None or reg["estado"] != "en_disputa":
                continue
            previo = arbitrajes.get(d["documento_id"]) or {}
            if previo.get("huella") == d["huella_pdf"] and previo.get("estado") == "ok":
                continue
            a = json.loads(reg["json_a"]) if reg.get("json_a") else None
            b = json.loads(reg["json_b"]) if reg.get("json_b") else None
            _, pendientes, encabezado = resolver_disputa(a, b, self.cfg)
            if pendientes or encabezado:
                candidatos.append((d, a, b))
        if not candidatos:
            return
        cliente = self.cliente
        if cliente is None and not os.environ.get("ANTHROPIC_API_KEY"):
            self.notas.append(f"arbitraje omitido: falta ANTHROPIC_API_KEY ({len(candidatos)} documentos en disputa)")
            return
        if cliente is None:
            cliente = dividendos.ClienteLLM(int(self.cfg.get("max_tokens", 16000)), herramienta=HERRAMIENTA)
        for d, a, b in candidatos[:maximo]:
            em = emisores[d["emisor_id"]]
            contexto = (f"Emisor: {em['nombre']}\nMoneda en que reporta: {em.get('moneda', 'COP')}\n"
                        f"Corte probable según el nombre del archivo: {d['corte'] or 'desconocido'}")
            try:
                pdf = unir_pdf([(data_dir() / x).read_bytes() for x in d["archivos"]])
                r = arbitrar(pdf, contexto, a, b, cliente, self._modelos_arbitro(), self.cfg)
            except Exception as e:  # noqa: BLE001 - se reintenta en la próxima corrida
                self.conteo["arbitraje_error"] = self.conteo.get("arbitraje_error", 0) + 1
                self.notas.append(f"arbitraje con error: {type(e).__name__}")
                continue
            arbitrajes[d["documento_id"]] = {**r, "huella": d["huella_pdf"], "fecha": self._hoy().isoformat()}
            decididas = sum(1 for v in r["decisiones"].values() if v)
            self.conteo["arbitrados"] = self.conteo.get("arbitrados", 0) + 1
            self.conteo["cifras_decididas_por_el_arbitro"] = self.conteo.get("cifras_decididas_por_el_arbitro", 0) + decididas
        if len(candidatos) > maximo:
            self.notas.append(f"{len(candidatos) - maximo} documentos en disputa esperan arbitraje")

    # ----------------------------------------------------------------- interfaz del conector
    def descargar(self, desde: date, hasta: date) -> list[dict]:
        self.conteo = {"bajados": 0, "manuales": 0, "fallidos": 0, "descargas_pospuestas": 0,
                       "demasiado_grandes": 0, "paginas_enviadas": 0, "indices_sin_enlaces": 0,
                       "emisores_en_pausa_por_bloqueo": 0, "desde_sfc": 0}
        self.bloqueados: set[str] = set()
        hoy = self._hoy()
        pausa = int(self.cfg.get("limites", {}).get("reintentar_404_dias", 7))
        for em in self.cfg["emisores"]:
            self._manuales(em)
            if em.get("estados_desde_sfc"):
                self._desde_sfc(em)
            if em.get("descarga") != "automatica":
                continue
            bloqueo = _fecha((self.estado_previo.get("bloqueos") or {}).get(em["emisor_id"], "")[:10])
            if bloqueo and (hoy - bloqueo).days < pausa and not self.forzado:
                self.conteo["emisores_en_pausa_por_bloqueo"] += 1
                continue
            for cand in self._descubrir(em, hoy):
                if em["emisor_id"] in self.bloqueados:
                    break
                self._bajar_pdf(em, cand)
        docs = self.documentos()
        log = storage.leer_tabla("silver", TABLA_EXTRACCION)
        leidos = {} if log is None else {(r["documento_id"], r["huella_pdf"]): r for r in log.iter_rows(named=True)}
        # Se repiten los errores y lo que no llegó a la API (sin páginas reconocibles: no cuesta)
        pendientes = [d for d in docs if (d["documento_id"], d["huella_pdf"]) not in leidos
                      or leidos[(d["documento_id"], d["huella_pdf"])]["estado"] in REINTENTAR]
        # Leídos con una versión anterior de la lectura (menos conceptos): después de los nuevos
        pendientes += [d for d in docs if (d["documento_id"], d["huella_pdf"]) in leidos
                       and leidos[(d["documento_id"], d["huella_pdf"])]["estado"] not in REINTENTAR
                       and version_de(leidos[(d["documento_id"], d["huella_pdf"])]) < VERSION_LECTURA]
        # Disputas leídas con otras páginas de las que se eligen hoy (Paz del Río jun-2026: antes solo se
        # reconocía una página de notas): se leen de nuevo con las páginas correctas antes del árbitro
        pendientes += [d for d in docs if d not in pendientes and self._paginas_cambiaron(d, leidos)]
        nuevas = self._leer(pendientes)
        if nuevas:
            storage.upsert_por_anio("silver", TABLA_EXTRACCION, pl.DataFrame(nuevas, schema=ESQUEMA_EXTRACCION),
                                    ("documento_id",), "fecha_referencia")
            leidos.update({(r["documento_id"], r["huella_pdf"]): r for r in nuevas})
        # Lo leído (en esta corrida o en una anterior que falló después de leer) que aún no está en
        # silver se incorpora sin volver a llamar a la API
        arbitrajes = self.estado_nuevo.setdefault("arbitrajes", {})
        self._arbitrar(docs, leidos, arbitrajes)
        incorporados = self.estado_nuevo.setdefault("incorporados", {})
        emisores = emisores_config(self.cfg)
        revision = leer_revision()
        registros, por_revisar, revisados = [], [], set()
        for d in docs:
            reg = leidos.get((d["documento_id"], d["huella_pdf"]))
            if reg is not None:
                revisados.add(d["documento_id"])
            arb = arbitrajes.get(d["documento_id"])
            arb = arb if arb and arb.get("huella") == d["huella_pdf"] else None
            if reg is not None and reg["estado"] == "en_disputa":
                por_revisar += filas_revision(d, reg, emisores[d["emisor_id"]], self.cfg, arb)
            # La marca cambia si cambia cómo se juzga el emisor (acepta_separados): se reincorpora sin API
            sep = int(bool(emisores[d["emisor_id"]].get("acepta_separados")))
            marca = f"{d['huella_pdf']}:v{version_de(reg)}:s{sep}" if reg is not None else None
            if marca and reg["estado"] == "en_disputa":
                # Una disputa se vuelve a incorporar si cambia la regla de resolución o una decisión a mano
                marca += f":r{RESOLUCION_VERSION}:{huella_revision(d['documento_id'], revision)}:{(arb or {}).get('modelo') or '-'}"
            if reg is None or reg["estado"] == "error" or (incorporados.get(d["documento_id"]) == marca and not self.forzado):
                continue
            registros += filas_documento(d, reg, emisores[d["emisor_id"]], self.cfg, revision, arb)
            incorporados[d["documento_id"]] = marca
        sin_decidir = [f for f in por_revisar if (f["documento_id"], f["concepto"]) not in revision]
        if exportar_revision(por_revisar, revisados) and sin_decidir:
            self.notas.append(f"{len(sin_decidir)} cifras en disputa por decidir en revision/{ARCHIVO_REVISION}")
        resumen = {k: v for k, v in self.conteo.items() if v}
        if resumen:
            self.notas.append(", ".join(f"{k} {v}" for k, v in resumen.items()))
        if self.bloqueados:
            self.notas.append(f"{len(self.bloqueados)} emisores con protección contra robots: PDF a mano en {CARPETA_MANUAL}/")
        if not registros and not nuevas:
            self.sin_novedades = True
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        filas = [{k: r.get(k) for k in ESQUEMA_SILVER} for r in registros]
        for f in filas:
            for k in ("fecha_corte", "fecha_publicacion", "fecha_descubierto"):
                f[k] = _fecha(f[k])
        return pl.DataFrame(filas, schema=ESQUEMA_SILVER)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        cuarentena = df.filter(pl.col("motivo").is_not_null())
        validas = df.filter(pl.col("motivo").is_null() & pl.col("estado").is_in(sorted(ESTADOS_ACEPTADOS))
                            & pl.col("valor").is_not_null() & pl.col("fecha_corte").is_not_null()).drop("motivo")
        return validas, cuarentena


def _neg(fecha: str | None) -> int:
    d = _fecha(fecha)
    return -(d.toordinal() if d else 0)
