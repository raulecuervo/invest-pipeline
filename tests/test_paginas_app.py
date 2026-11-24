"""Las páginas del aplicativo cargan sin errores en un Chromium sin ventana (scripts/probar_paginas.py).

Se salta si no están Playwright (extra ``pc``) y FastAPI (extra ``app``) o si Chromium no está instalado
(``playwright install chromium``). No usa internet: arma su propio conjunto de datos sintético y, si
existe la carpeta de demostración (``INVEST_DEMO_DIR`` o ``/tmp/demo``), prueba también una copia de ella.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

RAIZ = Path(__file__).resolve().parents[1]
DEMO = Path(os.environ.get("INVEST_DEMO_DIR", "/tmp/demo"))


@pytest.fixture(scope="module")
def probador():
    spec = importlib.util.spec_from_file_location("probar_paginas", RAIZ / "scripts" / "probar_paginas.py")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["probar_paginas"] = modulo
    spec.loader.exec_module(modulo)
    from playwright.sync_api import Error, sync_playwright

    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Error as e:
        pytest.skip(f"Chromium no disponible para Playwright: {str(e).splitlines()[0]}")
    return modulo


def _fallas(resultados) -> str:
    return "\n".join(f"#/{r.ruta}: " + "; ".join(r.problemas) for r in resultados if not r.ok)


def test_paginas_con_datos_sinteticos(carpeta_datos, probador):
    import app_datos_util

    app_datos_util.construir()
    resultados = probador.probar(carpeta_datos, python=sys.executable)
    rutas = {r.ruta for r in resultados}
    # Todas las páginas del menú, una ficha, las dos carteras por su id y el caso de especie inexistente
    assert set(probador.PAGINAS_FIJAS) <= rutas
    assert {"cartera/principal", "cartera/ensayo", probador.PAGINA_NEGATIVA} <= rutas
    assert any(r.startswith("ficha/coc") for r in rutas)
    assert all(r.ok for r in resultados), _fallas(resultados)
    assert next(r for r in resultados if r.ruta == "cartera/principal").titulo == "Mi cartera"


def test_paginas_sin_modelo_calculado(carpeta_datos, probador):
    """Una carpeta casi vacía (antes de la primera corrida de invest modelo) tampoco rompe las páginas."""
    import app_datos_util

    app_datos_util.dims()
    resultados = probador.probar(carpeta_datos, python=sys.executable, max_fichas=1)
    assert all(r.ok for r in resultados), _fallas(resultados)


@pytest.mark.skipif(not (DEMO / "gold").is_dir(), reason="sin carpeta de demostración")
def test_paginas_con_datos_de_demostracion(tmp_path, probador):
    # Copia: la base modelo.duckdb guarda rutas absolutas y el servidor la rehace en la copia
    copia = tmp_path / "demo"
    shutil.copytree(DEMO, copia, ignore=shutil.ignore_patterns("modelo.duckdb*", "bronze", "fuentes_web", "revision"))
    resultados = probador.probar(copia, python=sys.executable)
    assert all(r.ok for r in resultados), _fallas(resultados)
