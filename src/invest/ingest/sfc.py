"""Conectores de la Superintendencia Financiera (SFC): acciones inscritas en la Bolsa de Colombia.

La SFC publica, sin registro ni token, tres piezas por cada especie (acción):

1. Índice  ``ppal_promedios.htm``: lista de especies, incluidas las que ya no cotizan. Conservarlas
   evita el sesgo de supervivencia en las pruebas históricas.
2. Ficha   ``d_<especie>.htm``: código, sector, precio promedio ponderado, máximo y mínimo de dos
   meses, valor patrimonial, Q de Tobin, RPG, liquidez bursátil e IBA vigente.
3. Series  ``<especie>_<año>.xls`` (año en curso) y ``<especie>_ant.xls`` (años anteriores): precio
   promedio ponderado y valor transado por día, en formato Excel BIFF2.

La especie es el código de la página en minúsculas, p. ej. ``coc04paao008`` (Ecopetrol ordinaria):
    co c04 pa ao 008  ->  emisor ``coc04`` · clase ``ao`` (ordinaria) · consecutivo ``008``

Las páginas declaran iso-8859-1 pero usan caracteres de windows-1252, así que se decodifican así.
Rezago normal: uno o dos días hábiles.

Nota sobre la Bolsa de Colombia: su API (rest.bvc.com.co) exige un token de sesión que entrega
/api/handshake a su propia página web. No se replica ese mecanismo; la SFC es la fuente diaria.
"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

import polars as pl

from invest import biff2, storage, sucesiones
from invest.ingest import http
from invest.ingest.base import Conector

BASE = "https://www.superfinanciera.gov.co/info/superfinancierav8/media/MercadoAccionario/Precios/Diarios/"
URL_INDICE = BASE + "ppal_promedios.htm"
CODIFICACION = "cp1252"

# Una especie sin negociación en este número de días deja de consultarse a diario
# (se sigue leyendo en cada recarga histórica y si vuelve a aparecer en el índice con datos).
DIAS_INACTIVA = 400

CLASES = {"ao": "ordinaria", "ad": "preferencial", "ae": "sin_dividendo", "ap": "privilegiada"}
_RE_ESPECIE = re.compile(r"^([a-z0-9]{5})pa([a-z]{2})(\d{3})$")
_RE_ENLACE = re.compile(r'<a\s+href="d_([a-z0-9]+)\.htm"\s*>(.*?)</a>', re.I | re.S)
_RE_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_RE_TD = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
# La SFC a veces omite el cero inicial del día o el mes ("4/11/2025" en el archivo de 2025)
_RE_FECHA = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_RE_IBA = re.compile(r"^(ALT|MED|BAJ|MIN)\w*\s*:\s*([\d.,]+)", re.I)


# --------------------------------------------------------------------------- utilidades de texto
def decodificar(contenido: bytes) -> str:
    return contenido.decode(CODIFICACION, errors="replace")


def _sin_ruido(html: str) -> str:
    return re.sub(r"<!--.*?-->|<style\b.*?</style>|<script\b.*?</script>", " ", html, flags=re.S | re.I)


def texto(fragmento: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragmento)
    t = html_lib.unescape(t).replace("\xa0", " ")
    return " ".join(t.split())


def _normal(s: str) -> str:
    """Mayúsculas sin tildes ni espacios repetidos, para comparar etiquetas."""
    s = unicodedata.normalize("NFKD", s)
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).upper().split())


def fecha_dmy(valor: object) -> date | None:
    if valor is None:
        return None
    if isinstance(valor, (int, float)):  # número de serie de Excel, por si la SFC cambia el formato
        return date(1899, 12, 30) + timedelta(days=int(valor)) if valor > 0 else None
    m = _RE_FECHA.search(str(valor))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def numero(valor: object) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).replace(",", "").replace("%", "").strip()
    if not s or _normal(s) in {"N.A", "NA", "-", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def partes_especie(especie: str) -> tuple[str, str]:
    """(emisor, clase) a partir del código de la especie."""
    m = _RE_ESPECIE.match(especie)
    if not m:
        return especie[:5], "otra"
    return m.group(1), CLASES.get(m.group(2), "otra")


# --------------------------------------------------------------------------- índice de especies
def interpretar_indice(html: str) -> dict:
    """Devuelve {"fecha_reporte": date | None, "especies": [{especie, nombre, emisor, clase}]}."""
    limpio = _sin_ruido(html)
    m = re.search(r"Fecha de reporte\s*:\s*(\d{2}/\d{2}/\d{4})", texto(limpio), re.I)
    especies: list[dict] = []
    vistos: set[str] = set()
    for codigo, nombre_html in _RE_ENLACE.findall(limpio):
        especie = codigo.lower()
        nombre = texto(nombre_html)
        if nombre.endswith("..") or (nombre.endswith(".") and not nombre.endswith("S.A.")):
            nombre = nombre[:-1].rstrip()
        if nombre.startswith("(") and especies:
            # Enlace de nota que acompaña a la especie anterior, p. ej. "( Hasta el 31/12/2002 ...)"
            nombre = f"{especies[-1]['nombre']} {nombre}"
        if especie in vistos:
            continue
        vistos.add(especie)
        emisor, clase = partes_especie(especie)
        if _normal(nombre).startswith("DERECHOS"):
            clase = "derechos"
        especies.append({"especie": especie, "nombre": nombre, "emisor": emisor, "clase": clase})
    return {"fecha_reporte": fecha_dmy(m.group(1)) if m else None, "especies": especies}


# --------------------------------------------------------------------------- ficha de la especie
def _filas(html: str) -> list[list[str]]:
    return [[texto(td) for td in _RE_TD.findall(tr)] for tr in _RE_TR.findall(_sin_ruido(html))]


def _tabla_tras_encabezado(filas: list[list[str]], primera: str) -> tuple[list[str], list[str]] | None:
    """Encabezado cuya primera celda es ``primera``; devuelve (fila de fechas, fila de valores)."""
    for i, f in enumerate(filas):
        if f and _normal(f[0]) == primera and i + 2 < len(filas):
            return filas[i + 1], filas[i + 2]
    return None


def _etiqueta(filas: list[list[str]], etiqueta: str) -> str | None:
    for f in filas:
        if len(f) >= 2 and _normal(f[0]) == etiqueta:
            return f[1].strip() or None
    return None


def interpretar_ficha(html: str, especie: str) -> dict:
    filas = _filas(html)
    plano = texto(_sin_ruido(html))
    m_rep = re.search(r"Fecha\s+de\s+reporte\s*:\s*(\d{2}/\d{2}/\d{4})", plano, re.I)
    m_ult = re.search(r"Fecha\s+de\s+la\s+ultima\s+operaci\w+\s*:\s*(\d{2}/\d{2}/\d{4})", plano, re.I)
    m_nom = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    nombre = texto(m_nom.group(1)) if m_nom else None
    if nombre and " - " in nombre:
        nombre = nombre.rsplit(" - ", 1)[0]

    reg: dict = {
        "especie": especie,
        "nombre": nombre,
        "codigo_accion": _etiqueta(filas, "CODIGO DE LA ACCION"),
        "clase_accion_sfc": _etiqueta(filas, "CLASE DE ACCION"),
        "sector_sfc": _etiqueta(filas, "SECTOR ECONOMICO"),
        "fecha_reporte": fecha_dmy(m_rep.group(1)) if m_rep else None,
        "fecha_ultima_operacion": fecha_dmy(m_ult.group(1)) if m_ult else None,
    }

    precios = _tabla_tras_encabezado(filas, "PROMEDIO PONDERADO")
    campos_p = [("precio_promedio", "fecha_precio"), ("maximo_2m", "fecha_maximo"),
                ("minimo_2m", "fecha_minimo"), ("valor_patrimonial", "fecha_valor_patrimonial")]
    fechas, valores = precios if precios else ([], [])
    for i, (campo, campo_fecha) in enumerate(campos_p):
        reg[campo] = numero(valores[i]) if i < len(valores) else None
        reg[campo_fecha] = fecha_dmy(fechas[i]) if i < len(fechas) else None
    # La SFC escribe 1.00 cuando no tiene el valor patrimonial (misma convención que en las series).
    if reg["valor_patrimonial"] is not None and reg["valor_patrimonial"] <= 1.0:
        reg["valor_patrimonial"] = None

    indicadores = _tabla_tras_encabezado(filas, "Q-TOBIN")
    fechas, valores = indicadores if indicadores else ([], [])
    for i, (campo, campo_fecha) in enumerate(
        [("q_tobin", "fecha_q_tobin"), ("rpg", "fecha_rpg"), ("liquidez_bursatil", "fecha_liquidez")]
    ):
        reg[campo] = numero(valores[i]) if i < len(valores) else None
        reg[campo_fecha] = fecha_dmy(fechas[i]) if i < len(fechas) else None
    m_iba = _RE_IBA.match(valores[3].strip()) if len(valores) > 3 else None
    reg["iba_categoria"] = _normal(m_iba.group(1))[:3] if m_iba else None
    reg["iba"] = numero(m_iba.group(2)) if m_iba else None
    reg["fecha_iba"] = fecha_dmy(fechas[3]) if len(fechas) > 3 else None
    return reg


# --------------------------------------------------------------------------- especies a consultar
def especies_vigentes() -> list[str]:
    """Especies del último índice guardado; si aún no hay índice, lo descarga primero."""
    df = storage.leer_tabla("silver", ConectorSFCEspecies.tabla)
    if df is None or df.is_empty():
        ConectorSFCEspecies().ejecutar()
        df = storage.leer_tabla("silver", ConectorSFCEspecies.tabla)
    if df is None or df.is_empty():
        raise RuntimeError("No se pudo obtener el índice de especies de la SFC")
    ultimo = df.get_column("fecha_reporte").max()
    return df.filter(pl.col("fecha_reporte") == ultimo).get_column("especie").sort().to_list()


def ultima_negociacion() -> dict[str, date]:
    df = storage.leer_tabla("silver", ConectorSFCPrecios.tabla)
    if df is None or df.is_empty():
        return {}
    agg = df.group_by("especie").agg(pl.col("fecha").max())
    return dict(zip(agg.get_column("especie").to_list(), agg.get_column("fecha").to_list()))


def fecha_reporte_vigente() -> date | None:
    df = storage.leer_tabla("silver", ConectorSFCEspecies.tabla)
    return None if df is None or df.is_empty() else df.get_column("fecha_reporte").max()


def ultima_operacion_segun_ficha() -> dict[str, date]:
    """Última operación que declara la ficha más reciente de cada especie."""
    df = storage.leer_tabla("silver", "fact_indicadores_sfc")
    if df is None or df.is_empty():
        return {}
    agg = df.sort("fecha_reporte").group_by("especie").agg(pl.col("fecha_ultima_operacion").last())
    return {e: f for e, f in agg.iter_rows() if f is not None}


def _sin_reporte_nuevo(conector: Conector) -> bool:
    """True si la SFC no publicó un reporte nuevo desde la última corrida exitosa de ``conector``."""
    vigente = fecha_reporte_vigente()
    if vigente is not None:
        conector.estado_nuevo["fecha_reporte"] = vigente.isoformat()
    if conector.forzado or vigente is None:
        return False
    if conector.estado_previo.get("fecha_reporte") == vigente.isoformat():
        conector.sin_novedades = True
        conector.notas.append(f"sin reporte nuevo de la SFC (sigue el del {vigente:%d/%m/%Y})")
        return True
    return False


def _inactiva(ultima: date | None, hoy: date) -> bool:
    return ultima is not None and (hoy - ultima).days > DIAS_INACTIVA


def _hoy() -> date:
    return date.today()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- conectores
class ConectorSFCEspecies(Conector):
    """Índice de especies: una foto por fecha de reporte (permite ver altas y bajas)."""

    fuente_id = "sfc_especies"
    tabla = "dim_especie_sfc"
    claves = ("fecha_reporte", "especie")
    columna_fecha = "fecha_reporte"

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        contenido = self.bajar(URL_INDICE)
        if contenido is None:
            self.sin_novedades = True
            self.notas.append("el índice no cambió desde la última corrida")
            return []
        self.crudos["ppal_promedios.htm"] = contenido
        indice = interpretar_indice(decodificar(contenido))
        return [{**e, "fecha_reporte": indice["fecha_reporte"]} for e in indice["especies"]]

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        esquema = {
            "fecha_reporte": pl.Date, "especie": pl.Utf8, "nombre": pl.Utf8, "emisor": pl.Utf8,
            "clase": pl.Utf8, "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
        }
        ahora = _ahora()
        filas = [{**r, "fuente_id": self.fuente_id, "ingest_ts": ahora} for r in registros]
        return pl.DataFrame(filas, schema=esquema)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        malo = pl.col("fecha_reporte").is_null()
        return df.filter(~malo), df.filter(malo).with_columns(pl.lit("indice_sin_fecha").alias("motivo"))


class ConectorSFCPrecios(Conector):
    """Precio promedio ponderado y valor transado diarios por especie."""

    fuente_id = "sfc_precios_promedio"
    tabla = "fact_precio_sfc"
    claves = ("fecha", "especie")
    columna_fecha = "fecha"
    # Filas con precio 1 y valor 1: la SFC las usa para días sin negociación (p. ej. antes de que
    # la acción se listara). Van a cuarentena como evidencia, pero no son una alerta.
    motivos_esperados = {"marcador_sin_negociacion"}

    def _sufijos(self, desde: date, hasta: date, anio_actual: int) -> list[tuple[str, bool]]:
        """(sufijo del archivo, obligatorio)."""
        sufijos: list[tuple[str, bool]] = []
        if hasta.year >= anio_actual:
            sufijos.append((str(anio_actual), True))
        if desde.year < anio_actual:
            # A comienzos de año la SFC puede tardar en pasar el año anterior a _ant.
            sufijos.append((str(anio_actual - 1), False))
            sufijos.append(("ant", True))
        return sufijos

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        hoy = _hoy()
        if _sin_reporte_nuevo(self):
            return []
        historica = desde.year < hoy.year
        sufijos = self._sufijos(desde, hasta, hoy.year)
        ultimas = ultima_negociacion()
        operaciones = {} if self.forzado else ultima_operacion_segun_ficha()
        registros: list[dict] = []
        omitidas = sin_operacion = sin_cambio = faltantes = 0
        # Antecesoras de una sucesión (config/sucesiones.yaml): la SFC ya no las lista; se bajan una vez
        antecesoras = [e for e in sucesiones.especies_antecesoras() if e not in ultimas or self.forzado]
        for especie in especies_vigentes() + antecesoras:
            if especie in antecesoras:
                for sufijo, obligatorio in (("ant", True), ("2025", False)):
                    try:
                        contenido = self.bajar(BASE + f"{especie}_{sufijo}.xls", reintentos=2)
                    except http.AccesoDenegado:
                        raise
                    except http.ErrorHTTP:
                        faltantes += 1 if obligatorio else 0
                        continue
                    if contenido:
                        self.crudos[f"{especie}_{sufijo}.xls"] = contenido
                        registros += [{"especie": especie, "codigo_titulo": f.get("cd_titulo"), "fecha": fecha_dmy(f.get("fc_oper")),
                                       "precio_promedio": numero(f.get("pr_ponderado")), "valor_transado": numero(f.get("vr_transado"))}
                                      for f in biff2.leer_tabla(contenido)]
                continue
            if not historica and _inactiva(ultimas.get(especie), hoy):
                omitidas += 1
                continue
            # La ficha del día dice cuándo fue la última operación: si ya está guardada, no hay nada que bajar
            operacion, guardada = operaciones.get(especie), ultimas.get(especie)
            if not historica and operacion and guardada and operacion <= guardada:
                sin_operacion += 1
                continue
            for sufijo, obligatorio in sufijos:
                nombre = f"{especie}_{sufijo}.xls"
                try:
                    contenido = self.bajar(BASE + nombre, reintentos=4 if obligatorio else 1)
                except http.AccesoDenegado:
                    raise
                except http.ErrorHTTP:
                    if obligatorio:
                        faltantes += 1
                    continue
                if contenido is None:
                    sin_cambio += 1
                    continue
                self.crudos[nombre] = contenido
                for fila in biff2.leer_tabla(contenido):
                    fecha = fecha_dmy(fila.get("fc_oper"))
                    if fecha is not None and not (desde <= fecha <= hasta):
                        continue
                    registros.append(
                        {
                            "especie": especie,
                            "codigo_titulo": fila.get("cd_titulo"),
                            "fecha": fecha,
                            "precio_promedio": numero(fila.get("pr_ponderado")),
                            "valor_transado": numero(fila.get("vr_transado")),
                        }
                    )
        if omitidas:
            self.notas.append(f"{omitidas} especies inactivas omitidas")
        if sin_operacion:
            self.notas.append(f"{sin_operacion} sin operación nueva")
        if sin_cambio:
            self.notas.append(f"{sin_cambio} archivos sin cambios")
        if faltantes:
            self.notas.append(f"{faltantes} archivos no disponibles")
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        esquema = {
            "fecha": pl.Date, "especie": pl.Utf8, "codigo_titulo": pl.Utf8,
            "precio_promedio": pl.Float64, "valor_transado": pl.Float64,
            "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
        }
        ahora = _ahora()
        filas = [
            {**r, "codigo_titulo": str(r["codigo_titulo"]).strip() if r.get("codigo_titulo") else None,
             "fuente_id": self.fuente_id, "ingest_ts": ahora}
            for r in registros
        ]
        df = pl.DataFrame(filas, schema=esquema)
        # La SFC a veces repite un día con valores idénticos (visto el 24/03/2026): se deja una fila.
        return df.unique(
            subset=["fecha", "especie", "precio_promedio", "valor_transado"], keep="first", maintain_order=True
        )

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        conflicto = pl.len().over(["fecha", "especie"]) > 1
        motivo = (
            pl.when(pl.col("fecha").is_null()).then(pl.lit("fecha_ilegible"))
            .when(pl.col("precio_promedio").is_null() | (pl.col("precio_promedio") <= 0))
            .then(pl.lit("precio_no_positivo"))
            .when((pl.col("precio_promedio") <= 1) & (pl.col("valor_transado").fill_null(0) <= 1))
            .then(pl.lit("marcador_sin_negociacion"))
            .when(pl.col("codigo_titulo").is_not_null()
                  & (pl.col("codigo_titulo").str.to_lowercase() != pl.col("especie")))
            .then(pl.lit("codigo_no_coincide"))
            .when(conflicto).then(pl.lit("fecha_duplicada_con_valores_distintos"))
            .otherwise(pl.lit(None, dtype=pl.Utf8))
        )
        df = df.with_columns(motivo.alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())


ESQUEMA_FICHA = {
    "fecha_reporte": pl.Date, "especie": pl.Utf8, "nombre": pl.Utf8, "codigo_accion": pl.Utf8,
    "clase_accion_sfc": pl.Utf8, "sector_sfc": pl.Utf8, "fecha_ultima_operacion": pl.Date,
    "precio_promedio": pl.Float64, "fecha_precio": pl.Date,
    "maximo_2m": pl.Float64, "fecha_maximo": pl.Date, "minimo_2m": pl.Float64, "fecha_minimo": pl.Date,
    "valor_patrimonial": pl.Float64, "fecha_valor_patrimonial": pl.Date,
    "q_tobin": pl.Float64, "fecha_q_tobin": pl.Date, "rpg": pl.Float64, "fecha_rpg": pl.Date,
    "liquidez_bursatil": pl.Float64, "fecha_liquidez": pl.Date,
    "iba_categoria": pl.Utf8, "iba": pl.Float64, "fecha_iba": pl.Date,
    "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}


class ConectorSFCFicha(Conector):
    """Ficha diaria por especie: sector, indicadores de mercado e IBA vigente."""

    fuente_id = "sfc_ficha_especie"
    tabla = "fact_indicadores_sfc"
    claves = ("fecha_reporte", "especie")
    columna_fecha = "fecha_reporte"

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        hoy = _hoy()
        if _sin_reporte_nuevo(self):
            return []
        ultimas = ultima_negociacion()
        registros, omitidas, faltantes, sin_cambio = [], 0, 0, 0
        for especie in especies_vigentes():
            if _inactiva(ultimas.get(especie), hoy):
                omitidas += 1
                continue
            nombre = f"d_{especie}.htm"
            try:
                contenido = self.bajar(BASE + nombre)
            except http.NoEncontrado:
                faltantes += 1
                continue
            if contenido is None:
                sin_cambio += 1
                continue
            self.crudos[nombre] = contenido
            registros.append(interpretar_ficha(decodificar(contenido), especie))
        if omitidas:
            self.notas.append(f"{omitidas} especies inactivas omitidas")
        if sin_cambio:
            self.notas.append(f"{sin_cambio} fichas sin cambios")
        if faltantes:
            self.notas.append(f"{faltantes} fichas no disponibles")
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        ahora = _ahora()
        filas = [{**r, "fuente_id": self.fuente_id, "ingest_ts": ahora} for r in registros]
        return pl.DataFrame(filas, schema=ESQUEMA_FICHA)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        motivo = (
            pl.when(pl.col("fecha_reporte").is_null()).then(pl.lit("ficha_sin_fecha"))
            .when(pl.col("codigo_accion").is_null()).then(pl.lit("ficha_sin_codigo"))
            .when(pl.col("codigo_accion").str.to_lowercase() != pl.col("especie"))
            .then(pl.lit("codigo_no_coincide"))
            .otherwise(pl.lit(None, dtype=pl.Utf8))
        )
        df = df.with_columns(motivo.alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())


# --------------------------------------------------------------------------- inspección manual
def inspeccionar(especie: str = "coc04paao008") -> dict:
    """Descarga las tres piezas de una especie y devuelve un resumen, sin guardar nada.

    Sirve para confirmar en el PC, en la primera corrida, que la estructura de la SFC no cambió.
    """
    indice = interpretar_indice(decodificar(http.obtener(URL_INDICE)))
    ficha = interpretar_ficha(decodificar(http.obtener(BASE + f"d_{especie}.htm")), especie)
    anio = _hoy().year
    serie = biff2.leer_tabla(http.obtener(BASE + f"{especie}_{anio}.xls"))
    return {
        "indice": {"fecha_reporte": indice["fecha_reporte"], "especies": len(indice["especies"])},
        "ficha": ficha,
        "serie": {
            "archivo": f"{especie}_{anio}.xls",
            "columnas": list(serie[0].keys()) if serie else [],
            "filas": len(serie),
            "primera": serie[0] if serie else None,
            "ultima": serie[-1] if serie else None,
        },
    }
