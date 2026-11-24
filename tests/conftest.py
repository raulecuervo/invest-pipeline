import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def registros_trm():
    return json.loads((FIXTURES / "trm_muestra.json").read_text(encoding="utf-8"))


@pytest.fixture
def carpeta_datos(tmp_path, monkeypatch):
    destino = tmp_path / "invest-data"
    monkeypatch.setenv("INVEST_DATA_DIR", str(destino))
    return destino
