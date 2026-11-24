"""Información relevante de la SFC: dividendos, asambleas, OPA, readquisiciones, fusiones, etc.

Fuente: "Consulta todas las entidades" del Registro Nacional de Valores y Emisores
(ReportesInformacionRelevante). Es un formulario JSF (PrimeFaces) público, sin registro ni captcha:

1. GET a la página del formulario: entrega la cookie de sesión y el estado del formulario
   (``javax.faces.ViewState``).
2. POST con tema "Todos" y un rango de fechas (dd/mm/aaaa): responde la página de resultados con
   las primeras 20 filas y el total (``rowCount``).
3. Si hay más de 20, un POST de paginación (AJAX de PrimeFaces) pide hasta 2.000 filas de una vez.
4. El anexo de una fila (casi siempre un PDF) se pide con otro POST que nombra el índice de la fila.
   Solo se bajan los de dividendos y cambios de capital de emisores con acciones inscritas.

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

from invest import eventos, storage
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
MAX_MESES_PENDIENTES = 3   # meses que se vuelven a consultar por corrida para bajar anexos que faltan

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


def ventanas_mensuales(desde: date, hasta: date) -> list[tuple[date, date]]:
    ventanas, inicio = [], desde
    while inicio <= hasta:
        fin_mes = (inicio.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        fin = min(fin_mes, hasta)
        ventanas.append((inicio, fin))
        inicio = fin + timedelta(days=1)
    return ventanas


def meses_con_anexos_pendientes(previos: pl.DataFrame | None, hoy: date, emisores: dict, alias: dict,
                                cubiertos: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Meses (más recientes primero) con eventos que debían tener PDF guardado y aún no lo tienen,
    por ejemplo porque una corrida llegó al tope de anexos. No incluye los meses ya consultados."""
    if previos is None or previos.is_empty():
        return []
    candidatos = previos.filter(
        pl.col("tiene_anexo") & pl.col("anexo_archivo").is_null()
        & (pl.col("fecha") >= hoy - timedelta(days=DIAS_ANEXOS))
    )
    meses: set[date] = set()
    for fecha, entidad, tema, resumen in candidatos.select("fecha", "entidad", "tema", "resumen").iter_rows():
        if any(ini <= fecha <= fin for ini, fin in cubiertos):
            continue
        if eventos.clasificar(tema, resumen) in CATEGORIAS_CON_ANEXO and eventos.emisor_de(entidad, emisores, alias):
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
        pendientes = meses_con_anexos_pendientes(previos, hoy, emisores, alias, ventanas)
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
                    reg["anexo_archivo"] is None and f["anexo_control"] and d and (hoy - d).days <= DIAS_ANEXOS
                    and anexos < MAX_ANEXOS_POR_CORRIDA
                    and eventos.clasificar(f["tema"], f["resumen"]) in CATEGORIAS_CON_ANEXO
                    and eventos.emisor_de(f["entidad"], emisores, alias)
                )
                if quiere_anexo:
                    try:
                        nombre, contenido = self.cliente.anexo(ctx, f["anexo_control"])
                    except (http.ErrorHTTP, ErrorFormulario):
                        fallidos += 1
                    else:
                        relativa = f"sfc/{d.year}/{reg['evento_id']}_{_nombre_archivo(nombre)}"
                        storage.guardar_anexo(relativa, contenido)
                        reg["anexo_archivo"] = relativa
                        anexos += 1
                registros.append(reg)
        if anexos:
            self.notas.append(f"{anexos} anexos guardados")
        if fallidos:
            self.notas.append(f"{fallidos} anexos no se pudieron bajar")
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
