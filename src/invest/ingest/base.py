"""Interfaz común de los conectores.

Flujo de cada ejecución:
    descargar (fetch) -> guardar crudo en bronze -> interpretar (parse) -> validar
    -> filas válidas a silver (upsert por año) y filas rechazadas a cuarentena -> bitácora.

Los conectores son idempotentes: volver a correr el mismo rango produce el mismo resultado.

Corridas incrementales: sin ``--desde`` cada conector baja solo lo nuevo. Para eso guarda en
meta/estado_fuentes/<fuente>.json los validadores HTTP de cada URL (ETag, Last-Modified) y marcas
propias (p. ej. la última fecha de reporte procesada). Esa memoria se actualiza solo si la corrida
termina sin error, para no perder datos de una corrida fallida. Con ``--desde`` se fuerza la descarga.
"""

from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from invest import storage
from invest.ingest import http
from invest.config import fuente


@dataclass
class ResultadoIngesta:
    fuente_id: str
    desde: date
    hasta: date
    filas_recibidas: int = 0
    filas_validas: int = 0
    filas_cuarentena: int = 0
    archivo_bronze: Path | None = None
    particiones: list[Path] = field(default_factory=list)
    estado: str = "ok"
    detalle: str = ""

    def resumen(self) -> str:
        return (
            f"[{self.estado}] {self.fuente_id} {self.desde}..{self.hasta}: "
            f"{self.filas_recibidas} registros recibidos, {self.filas_validas} filas válidas, "
            f"{self.filas_cuarentena} en cuarentena"
            + (f" — {self.detalle}" if self.detalle else "")
        )


class Conector(ABC):
    fuente_id: str = ""
    tabla: str = ""
    claves: tuple[str, ...] = ()
    columna_fecha: str = "fecha"
    # Motivos de cuarentena que son normales en la fuente: se guardan, pero no marcan "revisar".
    motivos_esperados: set[str] = set()

    def __init__(self) -> None:
        self.meta = fuente(self.fuente_id)
        # Archivos crudos (nombre -> bytes) que ``descargar`` quiera conservar tal cual en bronze.
        # Si hay alguno, se guardan en un .zip en lugar del JSON de registros.
        self.crudos: dict[str, bytes] = {}
        self.notas: list[str] = []
        self.forzado = False
        self.sin_novedades = False
        self.estado_previo: dict = {}
        self.estado_nuevo: dict = {}

    def bajar(self, url: str, reintentos: int = 4, cabeceras: dict | None = None,
              timeout: int = 60) -> bytes | None:
        """Descarga ``url`` solo si cambió desde la última corrida exitosa (None si no cambió).
        En una corrida forzada siempre descarga."""
        previo = None if self.forzado else self.estado_previo.get("http", {}).get(url)
        extra = {k: v for k, v in (("timeout", timeout if timeout != 60 else None), ("cabeceras", cabeceras)) if v}
        contenido, validadores = http.obtener_condicional(url, previo, reintentos=reintentos, **extra)
        if validadores:
            self.estado_nuevo.setdefault("http", {})[url] = validadores
        return contenido

    @property
    def bronze_efimero(self) -> bool:
        efimero = not bool(self.meta.get("bronze_en_repositorio", True))
        if efimero and not self.meta.get("redistribuible", False):
            # El crudo efímero se sube como artefacto de un repo público: solo datos redistribuibles.
            raise ValueError(f"{self.fuente_id}: bronze_en_repositorio: false exige redistribuible: true")
        return efimero

    # --- a implementar por cada fuente -------------------------------------------------
    @abstractmethod
    def descargar(self, desde: date, hasta: date) -> list[dict]:
        """Trae los registros crudos de la fuente para el rango."""

    @abstractmethod
    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        """Convierte los registros crudos en filas tipadas de la tabla silver."""

    @abstractmethod
    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Devuelve (válidas, cuarentena). La cuarentena lleva una columna 'motivo'."""

    # --- comportamiento común ----------------------------------------------------------
    def rango_por_defecto(self, hoy: date | None = None) -> tuple[date, date]:
        """Desde el último dato guardado menos ``recarga_dias``; si no hay datos, todo el histórico."""
        hoy = hoy or date.today()
        existente = storage.leer_tabla("silver", self.tabla)
        if existente is None or existente.is_empty():
            desde = date.fromisoformat(str(self.meta.get("historico_desde", "2000-01-01")))
        else:
            ultimo = existente.get_column(self.columna_fecha).max()
            desde = ultimo - timedelta(days=int(self.meta.get("recarga_dias", 0)))
        return desde, hoy + timedelta(days=7)

    def ejecutar(self, desde: date | None = None, hasta: date | None = None) -> ResultadoIngesta:
        self.forzado = desde is not None
        if desde is None or hasta is None:
            d, h = self.rango_por_defecto()
            desde, hasta = desde or d, hasta or h
        res = ResultadoIngesta(self.fuente_id, desde, hasta)
        self.crudos, self.notas, self.sin_novedades = {}, [], False
        self.estado_previo = storage.leer_estado_fuente(self.fuente_id)
        self.estado_nuevo = copy.deepcopy(self.estado_previo)
        try:
            registros = self.descargar(desde, hasta)
            res.filas_recibidas = len(registros)
            if not registros and not self.crudos:
                pass  # nada nuevo: no se escribe un bronze vacío
            elif self.crudos:
                res.archivo_bronze = storage.guardar_bronze_zip(
                    self.fuente_id, self.crudos, efimero=self.bronze_efimero
                )
            else:
                res.archivo_bronze = storage.guardar_bronze(
                    self.fuente_id,
                    json.dumps(registros, ensure_ascii=False, default=str),
                    efimero=self.bronze_efimero,
                )
            df = self.interpretar(registros)
            validas, cuarentena = self.validar(df)
            res.filas_validas, res.filas_cuarentena = validas.height, cuarentena.height
            res.particiones = storage.upsert_por_anio(
                "silver", self.tabla, validas, self.claves, self.columna_fecha
            )
            storage.guardar_cuarentena(self.tabla, cuarentena)
            if res.filas_cuarentena:
                motivos = (
                    cuarentena.get_column("motivo").value_counts(sort=True)
                    if "motivo" in cuarentena.columns else None
                )
                inesperados = (
                    [m for m in motivos.get_column("motivo").to_list() if m not in self.motivos_esperados]
                    if motivos is not None else ["sin_motivo"]
                )
                if motivos is not None:
                    res.detalle = "cuarentena: " + ", ".join(
                        f"{m} {n}" for m, n in motivos.iter_rows()
                    )
                if inesperados:
                    res.estado = "revisar"
            if res.filas_recibidas == 0:
                res.estado = "sin_novedades" if self.sin_novedades else "sin_datos"
            if self.notas:
                res.detalle = "; ".join(filter(None, [res.detalle, *self.notas]))
            storage.guardar_estado_fuente(self.fuente_id, self.estado_nuevo)
        except Exception as e:  # la bitácora registra el fallo y la salud de datos lo muestra
            res.estado = "error"
            res.detalle = f"{type(e).__name__}: {e}"
        storage.registrar_ejecucion(
            {
                "fuente_id": res.fuente_id,
                "desde": res.desde,
                "hasta": res.hasta,
                "estado": res.estado,
                "filas_recibidas": res.filas_recibidas,
                "filas_validas": res.filas_validas,
                "filas_cuarentena": res.filas_cuarentena,
                "detalle": res.detalle,
            }
        )
        return res
