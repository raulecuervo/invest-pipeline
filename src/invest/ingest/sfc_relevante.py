"""Información relevante de la SFC: dividendos, asambleas, OPA, readquisiciones, fusiones, etc.

Fuente: "Consulta todas las entidades" del Registro Nacional de Valores y Emisores
(ReportesInformacionRelevante). Es un formulario JSF (PrimeFaces) público, sin registro ni captcha:

1. GET a la página del formulario: entrega la cookie de sesión y el estado del formulario
   (``javax.faces.ViewState``).
2. POST con tema "Todos" y un rango de fechas (dd/mm/aaaa): responde la página de resultados con
   las primeras 20 filas y el total (``rowCount``).
3. Si hay más de 20, un POST de paginación (AJAX de PrimeFaces) pide hasta 2.000 filas de una vez.
4. El anexo de una fila (casi siempre un PDF) se pide con otro POST que nombra el índice de la fila.
   Solo se bajan los de dividendos y cambios de capital de emisores con acciones inscritas, y los
   estados financieros (informe periódico trimestral o de fin de ejercicio) de los emisores que en
   ``config/estados_compania.yaml`` tienen ``estados_desde_sfc: true``: los que no transmiten XBRL
   ni publican sus estados en su sitio. ``invest.ingest.estados_compania`` los lee de ahí.

Se consulta mes a mes: en marzo, temporada de asambleas, llegan unos 1.200 registros.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urljoin

import polars as pl
import yaml

from invest import eventos, storage
from invest.config import CONFIG_DIR
from invest.ingest import http
from invest.ingest.base import Conector

URL_FORMULARIO = (
    "https://www.superfinanciera.gov.co/ReportesInformacionRelevante/faces/"
    "A_rnviRelevantes/consultas/consultaEntidades/consulta.xhtml"
)
FORM = "formaIngreso"
FILAS_POR_PEDIDO = 2000
# Anexos que se guardan: los que cambian la serie de precios o el retorno total
CATEGORIAS_CON_ANEXO = {"dividendo", "ex_dividendo", "capital", "escision_fusion", "opa"}
DIAS_ANEXOS = 400          # solo anexos recientes; el histórico completo pesaría cientos de MB
MAX_ANEXOS_POR_CORRIDA = 60
DIAS_ANEXOS_ESTADOS = 800   # estados financieros: dos cierres anuales y los trimestres entre ellos
_ES_ESTADOS = re.compile(r"ESTADOS? FINANCIER|INFORME PERIODICO|FIN DE EJERCICIO|INFORME FINANCIERO|EEFF")
_NO_ESTADOS = re.compile(r"PROYECTO|CONVOCATORIA|ASUNTOS SOCIALES|ENCUESTA|CODIGO PAIS|CALIFICACION")
_TEMA_ESTADOS = re.compile(r"INFORMES DE FIN DE EJERCICIO|NOTAS A ESTADOS|CORRECCIONES INFORMACION FINANCIERA")
_SI_ESTADOS = re.compile(r"ESTADOS? FINANCIER|INFORME PERIODICO")   # pesa más que _NO_ESTADOS
MAX_MB_ANEXO = 60   # un anexo más grande no se guarda (el repositorio de datos tiene tope de 100 MB por archivo)
_MESES = {m: i + 1 for i, m in enumerate(
    ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE",
     "NOVIEMBRE", "DICIEMBRE"])}
_TRIMESTRES = {"PRIMER": 1, "SEGUNDO": 2, "TERCER": 3, "CUARTO": 4}
MAX_MESES_PENDIENTES = 6   # meses que se vuelven a consultar por corrida para bajar anexos que faltan

_RE_VS = re.compile(r'name="javax\.faces\.ViewState"[^>]*value="([^"]*)"')
_RE_VS_PARCIAL = re.compile(r'<update id="[^"]*ViewState[^"]*"><!\[CDATA\[(.*?)\]\]>', re.S)
_RE_ACCION = re.compile(r'<form id="' + FORM + r'"[^>]*action="([^"]+)"')
_RE_TABLA = re.compile(r'<tbody id="(' + FORM + r':[^"]+)_data"')
_RE_TOTAL = re.compile(r"rowCount:(\d+)")
_RE_FILA = re.compile(r'<tr data-ri="(\d+)"[^>]*>(.*?)</tr>', re.S)
_RE_CELDA = re.compile(r'<td role="gridcell">(.*?)</td>', re.S)
_RE_ANEXO = re.compile(r'<a id="([^"]+:(\d+):[^"]+)"')


class ErrorFormulario(RuntimeError):
    """La página no tiene la forma esperada: la SFC pudo cambiar el formulario."""


@dataclass
class Contexto:
    """Estado del formulario después de una consulta; sirve para pedir los anexos de esas filas."""

    accion: str
    view_state: str


def _texto(fragmento: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragmento)
    return " ".join(html_lib.unescape(t).replace("\xa0", " ").split())


def interpretar_filas(html: str) -> list[dict]:
    filas = []
    for ri, contenido in _RE_FILA.findall(html):
        celdas = _RE_CELDA.findall(contenido)
        if len(celdas) < 6:
            continue
        anexo = _RE_ANEXO.search(celdas[5])
        filas.append(
            {
                "fila": int(ri),
                "fecha_txt": _texto(celdas[0]),
                "hora": _texto(celdas[1]),
                "entidad": _texto(celdas[2]),
                "tema": _texto(celdas[3]),
                "resumen": _texto(celdas[4]),
                "anexo_control": anexo.group(1) if anexo else None,
            }
        )
    return filas


def _vs(html: str) -> str:
    m = _RE_VS.search(html)
    if not m:
        raise ErrorFormulario("No se encontró javax.faces.ViewState")
    return html_lib.unescape(m.group(1))


def _accion(html: str, base: str) -> str:
    m = _RE_ACCION.search(html)
    if not m:
        raise ErrorFormulario("No se encontró el formulario formaIngreso")
    return urljoin(base, html_lib.unescape(m.group(1)))


def _txt(respuesta) -> str:
    # La SFC responde en UTF-8; no se confía en la adivinanza de codificación de requests.
    return respuesta.content.decode("utf-8", errors="replace")


class ClienteRelevante:
    def __init__(self, solicitar=None):
        self.solicitar = solicitar or http.solicitar

    def consultar(self, desde: date, hasta: date, tema: str = "Todos") -> tuple[list[dict], Contexto, str]:
        """Devuelve (filas, contexto para anexos, respuesta cruda con las filas)."""
        pagina = _txt(self.solicitar("GET", URL_FORMULARIO))
        datos = {
            FORM: FORM,
            f"{FORM}:tema_focus": "",
            f"{FORM}:tema_input": tema,
            f"{FORM}:fecini_input": desde.strftime("%d/%m/%Y"),
            f"{FORM}:fecfin_input": hasta.strftime("%d/%m/%Y"),
            f"{FORM}:cmdCertificar": "",
            f"{FORM}_SUBMIT": "1",
            "javax.faces.ViewState": _vs(pagina),
        }
        resultado = _txt(self.solicitar("POST", _accion(pagina, URL_FORMULARIO), datos=datos))
        total_m = _RE_TOTAL.search(resultado)
        tabla_m = _RE_TABLA.search(resultado)
        if not total_m or not tabla_m:
            raise ErrorFormulario("La respuesta de la consulta no trae la tabla de resultados")
        total, tabla = int(total_m.group(1)), tabla_m.group(1)
        ctx = Contexto(_accion(resultado, URL_FORMULARIO), _vs(resultado))
        filas = interpretar_filas(resultado)
        crudo = resultado
        if total > len(filas):
            filas, partes = [], []
            for primera in range(0, total, FILAS_POR_PEDIDO):
                parcial = _txt(self.solicitar(
                    "POST",
                    ctx.accion,
                    datos={
                        "javax.faces.partial.ajax": "true",
                        "javax.faces.source": tabla,
                        "javax.faces.partial.execute": tabla,
                        "javax.faces.partial.render": tabla,
                        tabla: tabla,
                        f"{tabla}_pagination": "true",
                        f"{tabla}_first": str(primera),
                        f"{tabla}_rows": str(FILAS_POR_PEDIDO),
                        f"{tabla}_encodeFeature": "true",
                        FORM: FORM,
                        "javax.faces.ViewState": ctx.view_state,
                    },
                    cabeceras={"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"},
                ))
                m = _RE_VS_PARCIAL.search(parcial)
                if m:
                    ctx.view_state = m.group(1)
                filas.extend(interpretar_filas(parcial))
                partes.append(parcial)
            crudo = "\n".join(partes)
        if len(filas) != total:
            raise ErrorFormulario(f"Se esperaban {total} filas y llegaron {len(filas)}")
        return filas, ctx, crudo

    def anexo(self, ctx: Contexto, control: str) -> tuple[str, bytes]:
        """Descarga el anexo de una fila; devuelve (nombre del archivo, bytes)."""
        r = self.solicitar(
            "POST",
            ctx.accion,
            datos={FORM: FORM, f"{FORM}_SUBMIT": "1", control: control, "javax.faces.ViewState": ctx.view_state},
        )
        disp = r.headers.get("content-disposition", "")
        m = re.search(r"filename\*=UTF-8''([^;]+)", disp) or re.search(r'filename="?([^";]+)', disp)
        nombre = unquote(m.group(1)) if m else "anexo.pdf"
        if "html" in r.headers.get("content-type", ""):
            raise ErrorFormulario("El anexo respondió una página en lugar de un archivo")
        return nombre, r.content


def evento_id(fecha: date, hora: str, entidad: str, tema: str, resumen: str) -> str:
    base = "|".join([fecha.isoformat(), hora, entidad, tema, resumen])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def emisores_con_estados_sfc() -> set[str]:
    """Emisores (``CO-XXXXX``) cuyos estados financieros se bajan de la información relevante."""
    ruta = CONFIG_DIR / "estados_compania.yaml"
    if not ruta.exists():
        return set()
    cfg = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    return {e["emisor_id"] for e in cfg.get("emisores") or [] if e.get("estados_desde_sfc")}


def es_informe_financiero(tema: str | None, resumen: str | None) -> bool:
    """Evento de "Informes de fin de ejercicio" que trae estados financieros (no el proyecto de
    utilidades, la convocatoria ni el capítulo de asuntos sociales y ambientales)."""
    r = eventos.normal(resumen)
    return (bool(_TEMA_ESTADOS.search(eventos.normal(tema))) and bool(_ES_ESTADOS.search(r))
            and (bool(_SI_ESTADOS.search(r)) or not _NO_ESTADOS.search(r)))


def _fin_trimestre(anio: int, t: int) -> date:
    return {1: date(anio, 3, 31), 2: date(anio, 6, 30), 3: date(anio, 9, 30), 4: date(anio, 12, 31)}[t]


def corte_de_resumen(resumen: str | None, publicado: date | None) -> date | None:
    """Fecha de corte que dice el resumen ("con corte al 30 de septiembre de 2026", "primer trimestre
    del 2026", "fin de ejercicio 2025"); si no la dice, el último fin de trimestre antes de publicarse
    (en enero-abril, el cierre del año anterior)."""
    t = eventos.normal(resumen)
    m = re.search(r"\b(\d{1,2}) DE (" + "|".join(_MESES) + r") (?:DE |DEL )?(?:ANO )?(\d{4})", t)
    if m:
        try:
            d = date(int(m.group(3)), _MESES[m.group(2)], int(m.group(1)))
        except ValueError:
            d = None
        if d and (d.month, d.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}:
            return d
    m = re.search(r"(PRIMER|SEGUNDO|TERCER|CUARTO) TRIMESTRE (?:DE |DEL )?(?:(?:EL )?ANO )?(\d{4})", t)
    if m:
        return _fin_trimestre(int(m.group(2)), _TRIMESTRES[m.group(1)])
    m = re.search(r"EJERCIC\w*\D{0,40}?(\d{4})", t)
    if m:
        return date(int(m.group(1)), 12, 31)
    if publicado is None:
        return None
    if publicado.month <= 4:
        return date(publicado.year - 1, 12, 31)
    for anio in (publicado.year, publicado.year - 1):
        for tr in (4, 3, 2, 1):
            if _fin_trimestre(anio, tr) < publicado:
                return _fin_trimestre(anio, tr)
    return None


def anexo_deseado(tema: str | None, resumen: str | None, entidad: str | None, fecha: date | None, hoy: date,
                  emisores: dict, alias: dict, con_estados: set[str]) -> bool:
    """Si el anexo de un evento se guarda: dividendos y capital de cualquier emisor con acciones, y
    estados financieros de los emisores de ``con_estados``."""
    if fecha is None:
        return False
    emisor = eventos.emisor_de(entidad, emisores, alias)
    if not emisor:
        return False
    dias = (hoy - fecha).days
    if dias <= DIAS_ANEXOS and eventos.clasificar(tema, resumen) in CATEGORIAS_CON_ANEXO:
        return True
    return (dias <= DIAS_ANEXOS_ESTADOS and f"CO-{emisor.upper()}" in con_estados
            and es_informe_financiero(tema, resumen))


def ventanas_mensuales(desde: date, hasta: date) -> list[tuple[date, date]]:
    ventanas, inicio = [], desde
    while inicio <= hasta:
        fin_mes = (inicio.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        fin = min(fin_mes, hasta)
        ventanas.append((inicio, fin))
        inicio = fin + timedelta(days=1)
    return ventanas


def meses_con_anexos_pendientes(previos: pl.DataFrame | None, hoy: date, emisores: dict, alias: dict,
                                cubiertos: list[tuple[date, date]],
                                con_estados: set[str] | None = None,
                                omitidos: dict | None = None) -> list[tuple[date, date]]:
    """Meses (más recientes primero) con eventos que debían tener PDF guardado y aún no lo tienen,
    por ejemplo porque una corrida llegó al tope de anexos. No incluye los meses ya consultados."""
    if previos is None or previos.is_empty():
        return []
    candidatos = previos.filter(
        pl.col("tiene_anexo") & pl.col("anexo_archivo").is_null()
        & (pl.col("fecha") >= hoy - timedelta(days=max(DIAS_ANEXOS, DIAS_ANEXOS_ESTADOS)))
    )
    meses: set[date] = set()
    for ev, fecha, entidad, tema, resumen in candidatos.select(
            "evento_id", "fecha", "entidad", "tema", "resumen").iter_rows():
        if ev in (omitidos or {}):
            continue
        if any(ini <= fecha <= fin for ini, fin in cubiertos):
            continue
        if anexo_deseado(tema, resumen, entidad, fecha, hoy, emisores, alias, con_estados or set()):
            meses.add(fecha.replace(day=1))
    salida = []
    for inicio in sorted(meses, reverse=True)[:MAX_MESES_PENDIENTES]:
        salida.extend(ventanas_mensuales(inicio, min(hoy, (inicio.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))))
    return salida


def _nombre_archivo(texto: str) -> str:
    base, punto, ext = texto.rpartition(".")
    if not punto or len(ext) > 5:
        base, ext = texto, "bin"
    limpio = re.sub(r"[^A-Za-z0-9_-]+", "_", eventos.normal(base))[:80].strip("_") or "anexo"
    return f"{limpio}.{ext.lower()}"


def _hoy() -> date:
    return date.today()


ESQUEMA = {
    "evento_id": pl.Utf8, "fecha": pl.Date, "hora": pl.Utf8, "entidad": pl.Utf8, "tema": pl.Utf8,
    "resumen": pl.Utf8, "tiene_anexo": pl.Boolean, "anexo_archivo": pl.Utf8,
    "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}


class ConectorSFCRelevante(Conector):
    fuente_id = "sfc_informacion_relevante"
    tabla = "fact_evento_sfc"
    claves = ("evento_id",)
    columna_fecha = "fecha"

    def __init__(self, cliente: ClienteRelevante | None = None) -> None:
        super().__init__()
        self.cliente = cliente or ClienteRelevante()

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        hoy = _hoy()
        hasta = min(hasta, hoy)
        emisores = eventos.tabla_emisores(storage.leer_tabla("silver", "dim_especie_sfc"))
        alias = eventos.alias_manuales()
        previos = storage.leer_tabla("silver", self.tabla)
        ya_guardados = (
            dict(previos.filter(pl.col("anexo_archivo").is_not_null())
                 .select("evento_id", "anexo_archivo").iter_rows())
            if previos is not None and not previos.is_empty() else {}
        )
        registros: list[dict] = []
        anexos = fallidos = 0
        http.nueva_sesion()
        ventanas = ventanas_mensuales(desde, hasta)
        con_estados = emisores_con_estados_sfc()
        omitidos = self.estado_nuevo.setdefault("anexos_omitidos", {})
        pendientes = meses_con_anexos_pendientes(previos, hoy, emisores, alias, ventanas, con_estados, omitidos)
        if pendientes:
            self.notas.append(f"{len(pendientes)} meses consultados de nuevo por anexos pendientes")
        for ini, fin in ventanas + pendientes:
            filas, ctx, crudo = self.cliente.consultar(ini, fin)
            self.crudos[f"relevante_{ini:%Y%m%d}_{fin:%Y%m%d}.html"] = crudo.encode("utf-8")
            for f in filas:
                d = datetime.strptime(f["fecha_txt"], "%d/%m/%Y").date() if re.fullmatch(
                    r"\d{1,2}/\d{1,2}/\d{4}", f["fecha_txt"]) else None
                reg = {
                    "evento_id": evento_id(d, f["hora"], f["entidad"], f["tema"], f["resumen"]) if d else None,
                    "fecha": d, "hora": f["hora"], "entidad": f["entidad"], "tema": f["tema"],
                    "resumen": f["resumen"], "tiene_anexo": f["anexo_control"] is not None,
                    "anexo_archivo": None,
                }
                reg["anexo_archivo"] = ya_guardados.get(reg["evento_id"])
                quiere_anexo = (
                    reg["anexo_archivo"] is None and f["anexo_control"] and anexos < MAX_ANEXOS_POR_CORRIDA
                    and reg["evento_id"] not in omitidos
                    and anexo_deseado(f["tema"], f["resumen"], f["entidad"], d, hoy, emisores, alias, con_estados)
                )
                if quiere_anexo:
                    try:
                        nombre, contenido = self.cliente.anexo(ctx, f["anexo_control"])
                    except (http.ErrorHTTP, ErrorFormulario):
                        fallidos += 1
                    else:
                        if len(contenido) > MAX_MB_ANEXO * 1024 * 1024:
                            omitidos[reg["evento_id"]] = f"{hoy.isoformat()} {len(contenido) // 2**20} MB"
                            registros.append(reg)
                            continue
                        relativa = f"sfc/{d.year}/{reg['evento_id']}_{_nombre_archivo(nombre)}"
                        storage.guardar_anexo(relativa, contenido)
                        reg["anexo_archivo"] = relativa
                        anexos += 1
                registros.append(reg)
        if anexos:
            self.notas.append(f"{anexos} anexos guardados")
        if fallidos:
            self.notas.append(f"{fallidos} anexos no se pudieron bajar")
        if omitidos:
            self.notas.append(f"{len(omitidos)} anexos omitidos por tamaño (más de {MAX_MB_ANEXO} MB)")
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        ahora = datetime.now(timezone.utc)
        filas = [{**r, "fuente_id": self.fuente_id, "ingest_ts": ahora} for r in registros]
        # Filas idénticas en la fuente (mismo instante, entidad, tema y texto) son un solo evento
        return pl.DataFrame(filas, schema=ESQUEMA).unique(subset=["evento_id"], keep="first", maintain_order=True)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        motivo = (
            pl.when(pl.col("fecha").is_null()).then(pl.lit("fecha_ilegible"))
            .when(pl.col("entidad").fill_null("") == "").then(pl.lit("sin_entidad"))
            .otherwise(pl.lit(None, dtype=pl.Utf8))
        )
        df = df.with_columns(motivo.alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
