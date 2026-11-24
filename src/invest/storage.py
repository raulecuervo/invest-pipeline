"""Almacenamiento en capas (medallón) sobre Parquet.

- bronze: respuesta cruda de la fuente, solo se añade, por fecha de ingesta.
- silver: tablas tipadas y validadas, particionadas por año. Solo se reescriben los años que cambian,
  para que el historial de Git no crezca con copias de archivos viejos.
- gold: tablas listas para Power BI y la app (dimensiones y agregados).
- cuarentena: filas que no pasaron la validación, para revisión manual.
- meta: bitácora de ejecuciones.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import polars as pl

from invest.config import data_dir


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def ruta_capa(capa: str) -> Path:
    ruta = data_dir() / capa
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


CARPETA_EFIMERA = "_efimero"


def guardar_bronze(
    fuente_id: str, contenido: str | bytes, extension: str = "json", efimero: bool = False
) -> Path:
    """Guarda la respuesta cruda tal cual llegó. Nunca se modifica después.

    ``efimero=True`` la deja en ``bronze/_efimero/``, carpeta que invest-data no versiona en Git:
    sirve para fuentes de mucho volumen diario (p. ej. 117 archivos de la SFC). En GitHub Actions
    esa carpeta se sube como artefacto del workflow con retención de 30 días.
    """
    ahora = _ahora_utc()
    raiz = ruta_capa("bronze") / CARPETA_EFIMERA if efimero else ruta_capa("bronze")
    carpeta = raiz / fuente_id / f"fecha_ingesta={ahora:%Y-%m-%d}"
    carpeta.mkdir(parents=True, exist_ok=True)
    archivo = carpeta / f"{fuente_id}_{ahora:%H%M%S}.{extension}"
    modo = "wb" if isinstance(contenido, bytes) else "w"
    with open(archivo, modo, **({} if modo == "wb" else {"encoding": "utf-8"})) as f:
        f.write(contenido)
    return archivo


def guardar_bronze_zip(fuente_id: str, archivos: dict[str, bytes], efimero: bool = False) -> Path:
    """Guarda varios archivos crudos de una misma ejecución en un único .zip."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for nombre, datos in archivos.items():
            z.writestr(nombre, datos)
    return guardar_bronze(fuente_id, buffer.getvalue(), "zip", efimero=efimero)


def guardar_anexo(ruta_relativa: str, contenido: bytes) -> Path:
    """Guarda un documento de soporte (p. ej. el PDF de un proyecto de utilidades) en anexos/."""
    destino = data_dir() / "anexos" / ruta_relativa
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(contenido)
    return destino


def _carpeta_tabla(capa: str, tabla: str) -> Path:
    carpeta = ruta_capa(capa) / tabla
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def leer_tabla(capa: str, tabla: str) -> pl.DataFrame | None:
    carpeta = data_dir() / capa / tabla
    archivos = sorted(carpeta.glob("**/*.parquet")) if carpeta.exists() else []
    if not archivos:
        return None
    return pl.concat([pl.read_parquet(a) for a in archivos], how="vertical_relaxed")


def leer_dim(nombre: str) -> pl.DataFrame | None:
    """Lee una dimensión de gold/dims (dim_activo, dim_fecha...)."""
    archivo = data_dir() / "gold" / "dims" / f"{nombre}.parquet"
    return pl.read_parquet(archivo) if archivo.exists() else None


def upsert_por_anio(
    capa: str,
    tabla: str,
    df: pl.DataFrame,
    claves: Iterable[str],
    columna_fecha: str = "fecha",
) -> list[Path]:
    """Inserta o reemplaza filas por clave, reescribiendo solo las particiones de año afectadas."""
    claves = list(claves)
    if df.is_empty():
        return []
    carpeta = _carpeta_tabla(capa, tabla)
    df = df.with_columns(pl.col(columna_fecha).dt.year().alias("_anio"))
    escritos: list[Path] = []
    for (anio,), parte in df.group_by(["_anio"], maintain_order=True):
        parte = parte.drop("_anio")
        destino_dir = carpeta / f"anio={anio}"
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino = destino_dir / "parte.parquet"
        if destino.exists():
            existente = pl.read_parquet(destino)
            parte = pl.concat([existente, parte], how="vertical_relaxed")
        parte = parte.unique(subset=claves, keep="last", maintain_order=True).sort(claves)
        parte.write_parquet(destino)
        escritos.append(destino)
    return escritos


def escribir_por_anio(capa: str, tabla: str, df: pl.DataFrame, columna_fecha: str = "fecha") -> list[Path]:
    """Reescribe una tabla derivada partida por año, tocando solo los años cuyo contenido cambió.

    Para tablas grandes que se reconstruyen en cada corrida (gold): si el contenido de un año es
    igual al guardado, el archivo no se toca y Git no ve cambios. Borra los años que ya no existen
    y cualquier archivo suelto de una versión anterior sin particiones.
    """
    carpeta = _carpeta_tabla(capa, tabla)
    df = df.with_columns(pl.col(columna_fecha).dt.year().alias("_anio"))
    anios = set()
    escritos: list[Path] = []
    for (anio,), parte in df.group_by(["_anio"], maintain_order=True):
        anios.add(f"anio={anio}")
        parte = parte.drop("_anio")
        destino = carpeta / f"anio={anio}" / "parte.parquet"
        if destino.exists():
            try:
                if pl.read_parquet(destino).equals(parte):
                    continue
            except Exception:
                pass  # archivo ilegible o de otro esquema: se reescribe
        destino.parent.mkdir(parents=True, exist_ok=True)
        parte.write_parquet(destino)
        escritos.append(destino)
    for sobrante in carpeta.iterdir():
        if sobrante.is_file() and sobrante.suffix == ".parquet":
            sobrante.unlink()
        elif sobrante.is_dir() and sobrante.name.startswith("anio=") and sobrante.name not in anios:
            for f in sobrante.glob("*"):
                f.unlink()
            sobrante.rmdir()
    return escritos


def escribir_completa(capa: str, tabla: str, df: pl.DataFrame) -> Path:
    """Reescribe una tabla pequeña completa (dimensiones)."""
    carpeta = _carpeta_tabla(capa, "dims" if capa == "gold" and tabla.startswith("dim_") else tabla)
    destino = carpeta / f"{tabla}.parquet"
    df.write_parquet(destino)
    return destino


def guardar_cuarentena(tabla: str, df: pl.DataFrame, motivo_col: str = "motivo") -> Path | None:
    if df.is_empty():
        return None
    carpeta = _carpeta_tabla("cuarentena", tabla)
    destino = carpeta / f"{tabla}_{_ahora_utc():%Y%m%dT%H%M%S}.parquet"
    df.write_parquet(destino)
    return destino


def registrar_ejecucion(registro: dict) -> None:
    """Añade una línea a la bitácora de ejecuciones (meta/ejecuciones.jsonl)."""
    carpeta = ruta_capa("meta")
    registro = {"ts_utc": _ahora_utc().isoformat(timespec="seconds"), **registro}
    with open(carpeta / "ejecuciones.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")


def leer_estado_fuente(fuente_id: str) -> dict:
    """Memoria de descargas de una fuente: validadores HTTP por URL y marcas como la última fecha de
    reporte procesada. Permite que cada corrida baje solo lo nuevo."""
    archivo = data_dir() / "meta" / "estado_fuentes" / f"{fuente_id}.json"
    if not archivo.exists():
        return {}
    return json.loads(archivo.read_text(encoding="utf-8"))


def guardar_estado_fuente(fuente_id: str, estado: dict) -> None:
    carpeta = ruta_capa("meta") / "estado_fuentes"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / f"{fuente_id}.json").write_text(
        json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8"
    )


def leer_ejecuciones() -> list[dict]:
    archivo = data_dir() / "meta" / "ejecuciones.jsonl"
    if not archivo.exists():
        return []
    with open(archivo, encoding="utf-8") as f:
        return [json.loads(linea) for linea in f if linea.strip()]
