"""Eventos corporativos: clasificación por reglas y enlace con el emisor de la acción.

La SFC publica la "información relevante" con un tema (lista cerrada de la propia SFC) y un resumen
libre. Aquí se asigna una categoría de trabajo con reglas simples y legibles, en este orden:

    ex_dividendo · dividendo · opa · readquisicion · escision_fusion · capital · suspension ·
    desliste · asamblea_convocatoria · asamblea_decision · resultados · calificacion · gobierno ·
    emision_deuda · otros

Las categorías que pueden mover el precio o exigir un ajuste de la serie (dividendos, cambios de
capital, fusiones, OPA, suspensiones) se marcan con ``afecta_precio``.

El emisor se enlaza comparando el nombre de la entidad con los nombres del índice de especies de la
SFC, ya normalizados (sin tildes, sin prefijos de clase como "AO" o "ADP", sin "S.A.", "E.S.P." ni la
cola "ANTES ..."). Solo se acepta la coincidencia exacta; lo que no coincide se corrige a mano en
config/emisores_alias.yaml.
"""

from __future__ import annotations

import re
import unicodedata

import polars as pl
import yaml

from invest import storage
from invest.config import CONFIG_DIR

AFECTAN_PRECIO = {"ex_dividendo", "dividendo", "opa", "readquisicion", "escision_fusion", "capital",
                  "suspension", "desliste"}

# (categoría, patrón sobre el tema, patrón sobre el resumen); el primero que aplica gana.
REGLAS: list[tuple[str, str | None, str | None]] = [
    ("ex_dividendo", None, r"EX[- ]?DIVIDENDO"),
    ("dividendo", r"^PROYECTO UTILIDAD|ATRASOS EN PAGO",
     r"DIVIDENDO|DISTRIBUCION DE (LAS )?UTILIDADES|PROYECTO DE UTILIDADES"),
    ("opa", r"OFERTAS? PUBLICAS? DE ADQUISICION", r"OFERTA PUBLICA DE ADQUISICION|\bOPA\b"),
    ("readquisicion", r"READQUISICION", r"READQUISICION DE (LAS |SUS )?ACCIONES|RECOMPRA DE ACCIONES"),
    ("escision_fusion", r"^ESCISION|^FUSION", r"\bESCISION\b|\bFUSION\b"),
    ("capital", r"AUMENTO DEL CAPITAL|CAPITALIZACION|VALOR NOMINAL",
     r"EMISION DE ACCIONES|SPLIT|DESDOBLAMIENTO|DIVIDENDO EN ACCIONES"),
    ("suspension", r"SUSPENSION EN LA NEGOCIACION", None),
    ("desliste", r"DESLISTE|CANCELACION DE VALORES", r"CANCELACION DE LA INSCRIPCION"),
    ("asamblea_convocatoria", r"CONVOCATORIA|CITACION A ASAMBLEA", None),
    ("asamblea_decision", r"DECISIONES (ADOPTADAS POR LA )?(ASAMBLEA|DE ASAMBLEA)|DECISIONES DE ASAMBLEA", None),
    ("resultados", r"INFORMES DE FIN DE EJERCICIO|NOTAS A ESTADOS|CORRECCIONES INFORMACION FINANCIERA|"
                   r"SALVEDADES|CAMBIO EN POLITICAS CONTABLES", None),
    ("calificacion", r"CALIFICACION", None),
    ("gobierno", r"DIRECTORES|JUNTA DIRECTIVA|REPRESENTANTES? LEGAL|REVISOR FISCAL|BUEN GOBIERNO", None),
    ("emision_deuda", r"EMISION DE VALORES|PROSPECTOS|COLOCACION|TENEDORES DE BONOS", None),
]
_REGLAS = [(c, re.compile(t) if t else None, re.compile(r) if r else None) for c, t, r in REGLAS]

_PREFIJO_CLASE = re.compile(
    r"^(ACCION(ES)?\s+(ORDINARIAS\s+)?SIN\s+DIVIDENDO(\s+EN\s+VIRTUD\s+DE\s+LA\s+FUSION)?"
    r"|A[O0]\s+SIN\s+(DERECHO\s+A\s+)?DIVIDENDO|ADP\s+SIN\s+DERECHO\s+A\s+(VOTO|DIVIDENDO)"
    r"|ADP|A[O0]|AP)\s+(PEC\s+)?"
)
_COLA = re.compile(r"\(?\s*\bANT(ES\b|\.)|\bPODRA\b|\bPUDIENDO\b|\bEN\s+LIQUIDACION\b|\s-\s|\sO\s|,")
_SOBRAN = {"SA", "SAS", "ESP", "BIC"}


def normal(texto: str | None) -> str:
    """Mayúsculas sin tildes ni espacios repetidos."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", texto)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.upper().split())


def clasificar(tema: str | None, resumen: str | None) -> str:
    t, r = normal(tema), normal(resumen)
    for categoria, re_tema, re_resumen in _REGLAS:
        if (re_tema and re_tema.search(t)) or (re_resumen and re_resumen.search(r)):
            return categoria
    return "otros"


def clave_nombre(nombre: str | None, es_especie: bool = False) -> str:
    """Nombre reducido a lo esencial para comparar entidad (información relevante) y especie (índice)."""
    t = normal(nombre)
    if es_especie:
        t = _PREFIJO_CLASE.sub("", t)
    m = _COLA.search(t)
    if m and m.start() > 0:
        t = t[: m.start()]
    fichas = [f for f in re.sub(r"[^A-Z0-9]+", " ", t).split() if len(f) > 1 and f not in _SOBRAN]
    return " ".join(fichas)


def tabla_emisores(especies: pl.DataFrame | None) -> dict[str, str]:
    """clave de nombre -> código de emisor. Las claves que apuntan a dos emisores se descartan."""
    if especies is None or especies.is_empty():
        return {}
    ultimo = especies.get_column("fecha_reporte").max()
    vigentes = especies.filter((pl.col("fecha_reporte") == ultimo) & (pl.col("clase") != "derechos"))
    mapa: dict[str, set[str]] = {}
    for nombre, emisor in vigentes.select("nombre", "emisor").iter_rows():
        clave = clave_nombre(nombre, es_especie=True)
        if clave:
            mapa.setdefault(clave, set()).add(emisor)
    return {k: next(iter(v)) for k, v in mapa.items() if len(v) == 1}


def alias_manuales() -> dict[str, str]:
    archivo = CONFIG_DIR / "emisores_alias.yaml"
    if not archivo.exists():
        return {}
    datos = yaml.safe_load(archivo.read_text(encoding="utf-8"))
    return {clave_nombre(a["entidad"]): a["emisor"] for a in (datos or {}).get("alias", []) if a.get("emisor")}


def emisor_de(entidad: str | None, emisores: dict[str, str], alias: dict[str, str] | None = None) -> str | None:
    clave = clave_nombre(entidad)
    if alias and clave in alias:
        return alias[clave]
    return emisores.get(clave)


def construir_gold() -> pl.DataFrame | None:
    """gold/fact_evento_corporativo: eventos de silver + categoría + emisor_id."""
    eventos = storage.leer_tabla("silver", "fact_evento_sfc")
    if eventos is None or eventos.is_empty():
        return None
    emisores = tabla_emisores(storage.leer_tabla("silver", "dim_especie_sfc"))
    alias = alias_manuales()
    filas = eventos.select("entidad", "tema", "resumen").to_dicts()
    categorias = [clasificar(f["tema"], f["resumen"]) for f in filas]
    emisor = [emisor_de(f["entidad"], emisores, alias) for f in filas]
    resultado = eventos.with_columns(
        pl.Series("categoria", categorias, dtype=pl.Utf8),
        pl.Series("emisor", emisor, dtype=pl.Utf8),
    ).with_columns(
        pl.col("categoria").is_in(list(AFECTAN_PRECIO)).alias("afecta_precio"),
        pl.when(pl.col("emisor").is_not_null())
        .then("CO-" + pl.col("emisor").str.to_uppercase())
        .otherwise(None).alias("emisor_id"),
    ).drop("emisor").sort(["fecha", "hora", "evento_id"], descending=True)
    # Por año y solo lo que cambió: la tabla pesa varios MB y se versiona en Git todos los días
    storage.escribir_por_anio("gold", "fact_evento_corporativo", resultado, "fecha")
    return resultado
