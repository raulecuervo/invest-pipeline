"""Rutas y lectura de configuración.

El código vive en el repo público ``invest-pipeline``; los datos en el repo privado ``invest-data``.
La carpeta de datos se toma de la variable de entorno ``INVEST_DATA_DIR`` y, si no existe,
de una carpeta hermana ``../invest-data``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[2]
CONFIG_DIR = RAIZ / "config"


def data_dir() -> Path:
    ruta = os.environ.get("INVEST_DATA_DIR")
    base = Path(ruta) if ruta else RAIZ.parent / "invest-data"
    return base.resolve()


@lru_cache(maxsize=None)
def cargar_yaml(nombre: str) -> dict[str, Any]:
    with open(CONFIG_DIR / nombre, encoding="utf-8") as f:
        return yaml.safe_load(f)


def catalogo_fuentes() -> list[dict[str, Any]]:
    return cargar_yaml("catalogo_fuentes.yaml")["fuentes"]


def fuente(fuente_id: str) -> dict[str, Any]:
    for f in catalogo_fuentes():
        if f["id"] == fuente_id:
            return f
    raise KeyError(f"La fuente '{fuente_id}' no está en config/catalogo_fuentes.yaml")


def convenciones() -> dict[str, Any]:
    return cargar_yaml("convenciones.yaml")


def dimensiones() -> dict[str, Any]:
    return cargar_yaml("dimensiones.yaml")
