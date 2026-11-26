"""Catálogo de los documentos descargados (PDF y anexos) para consultarlos en el aplicativo.

Junta tres orígenes de ``invest-data``:

- ``anexos/companias/<emisor>/<corte>_<nombre>.pdf``: estados financieros del sitio de la compañía.
- ``fuentes_compania/<emisor>/``: PDF puestos a mano (sitios con protección contra robots).
- ``anexos/sfc/<año>/<evento>_<nombre>``: anexos de la información relevante de la Superfinanciera,
  con el emisor, la fecha y la categoría del evento (dividendos, estados financieros, capital...).

Cada documento queda con categoría, emisor, año, fecha, título, origen y si ya se leyó.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import polars as pl

from invest import eventos, storage
from invest.config import data_dir

CATEGORIA_EVENTO = {
    "resultados": "Estados financieros",
    "dividendo": "Dividendos", "ex_dividendo": "Dividendos",
    "capital": "Capital, escisiones y OPA", "escision_fusion": "Capital, escisiones y OPA",
    "opa": "Capital, escisiones y OPA", "readquisicion": "Capital, escisiones y OPA",
    "asamblea_convocatoria": "Asambleas", "asamblea_decision": "Asambleas",
    "calificacion": "Calificaciones", "gobierno": "Gobierno corporativo", "emision_deuda": "Emisiones de deuda",
}
EXTENSIONES = {".pdf", ".zip", ".docx", ".doc", ".xlsx", ".xls"}
_CORTE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)$")


def _fecha(texto: str | None) -> date | None:
    try:
        return date.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def _titulo_archivo(nombre: str) -> str:
    base = Path(nombre).stem
    m = _CORTE.match(base)
    base = m.group(2) if m else base
    return re.sub(r"[_]+", " ", base).strip()


def _lecturas() -> dict[str, str]:
    """{archivo: estado de su última lectura} (consenso, en_disputa, sin_paginas...) de los estados de compañías."""
    log = storage.leer_tabla("silver", "fact_ee_compania_extraccion")
    salida: dict[str, str] = {}
    if log is None or log.is_empty() or "archivos" not in log.columns:
        return salida
    for archivos, est in log.sort("procesado_ts").select("archivos", "estado").iter_rows():
        for a in (archivos or "").split(";"):
            if a:
                salida[a] = est
    return salida


def catalogo() -> pl.DataFrame:
    """Un renglón por archivo descargado: categoria, emisor_id, anio, fecha, titulo, origen, archivo
    (ruta dentro de invest-data), tipo, kb, lectura."""
    base = data_dir()
    filas: list[dict] = []
    vistos: set[str] = set()
    try:
        lecturas = _lecturas()
    except Exception:  # noqa: BLE001 - el catálogo no depende de que la lectura esté al día
        lecturas = {}

    def agregar(ruta: Path, **campos):
        rel = ruta.relative_to(base).as_posix()
        if rel in vistos or ruta.suffix.lower() not in EXTENSIONES:
            return
        vistos.add(rel)
        f = campos.get("fecha")
        filas.append({"categoria": campos.get("categoria") or "Otros", "emisor_id": campos.get("emisor_id"),
                      "anio": f.year if f else None, "fecha": f, "titulo": campos.get("titulo") or _titulo_archivo(ruta.name),
                      "origen": campos["origen"], "archivo": rel, "tipo": ruta.suffix.lower().lstrip("."),
                      "kb": round(ruta.stat().st_size / 1024), "lectura": lecturas.get(rel)})

    # 1. Información relevante de la SFC (con su evento: emisor, fecha y categoría)
    ev = storage.leer_tabla("silver", "fact_evento_sfc")
    if ev is not None and not ev.is_empty():
        emisores = eventos.tabla_emisores(storage.leer_tabla("silver", "dim_especie_sfc"))
        alias = eventos.alias_manuales()
        for r in ev.filter(pl.col("anexo_archivo").is_not_null()).iter_rows(named=True):
            ruta = base / "anexos" / r["anexo_archivo"]
            if not ruta.exists():
                continue
            emisor = eventos.emisor_de(r["entidad"], emisores, alias)
            cat = CATEGORIA_EVENTO.get(eventos.clasificar(r["tema"], r["resumen"]) or "", "Otros")
            resumen = re.sub(r"\s+", " ", r["resumen"] or "").strip()
            agregar(ruta, categoria=cat, emisor_id=f"CO-{emisor.upper()}" if emisor else None, fecha=r["fecha"],
                    titulo=(resumen[:160] + "…") if len(resumen) > 160 else resumen or r["tema"],
                    origen="Superfinanciera")
    # 2. Estados financieros del sitio de cada compañía y los puestos a mano
    for carpeta, origen in (("anexos/companias", "Sitio de la compañía"), ("fuentes_compania", "Puesto a mano")):
        raiz = base / carpeta
        if not raiz.exists():
            continue
        for ruta in sorted(raiz.glob("*/*")):
            if not ruta.is_file():
                continue
            m = _CORTE.match(ruta.stem)
            corte = _fecha(m.group(1)) if m else None
            resto = m.group(2) if m else ruta.stem
            # Los que vinieron de un anexo de la SFC se guardan como <corte>_sfc_<evento>
            desde_sfc = resto.startswith("sfc_")
            titulo = (f"Estados financieros al {corte.isoformat()}" if corte else _titulo_archivo(ruta.name)) + (
                "" if desde_sfc or not m else f" · {_titulo_archivo(ruta.name)}")
            agregar(ruta, categoria="Estados financieros", emisor_id=ruta.parent.name, fecha=corte, titulo=titulo,
                    origen="Superfinanciera (estados leídos)" if desde_sfc else origen)
    # 3. Lo que quede en la carpeta de la SFC sin evento conocido
    raiz = base / "anexos" / "sfc"
    if raiz.exists():
        for ruta in sorted(raiz.glob("*/*")):
            if ruta.is_file():
                anio = int(ruta.parent.name) if ruta.parent.name.isdigit() else None
                agregar(ruta, fecha=date(anio, 1, 1) if anio else None, origen="Superfinanciera")
    esquema = {"categoria": pl.Utf8, "emisor_id": pl.Utf8, "anio": pl.Int32, "fecha": pl.Date, "titulo": pl.Utf8,
               "origen": pl.Utf8, "archivo": pl.Utf8, "tipo": pl.Utf8, "kb": pl.Int64, "lectura": pl.Utf8}
    return pl.DataFrame(filas, schema=esquema).sort(["fecha", "archivo"], descending=[True, False], nulls_last=True)


def ruta_segura(archivo: str) -> Path:
    """Ruta absoluta de un archivo del catálogo; solo dentro de anexos/ o fuentes_compania/."""
    base = data_dir().resolve()
    ruta = (base / archivo).resolve()
    permitido = any(ruta.is_relative_to(base / c) for c in ("anexos", "fuentes_compania"))
    if not permitido or not ruta.is_file() or ruta.suffix.lower() not in EXTENSIONES:
        raise ValueError("archivo fuera del catálogo")
    return ruta
