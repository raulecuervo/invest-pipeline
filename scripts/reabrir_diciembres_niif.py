"""Vuelve a poner en la cola de la carga NIIF los cierres de diciembre que quedaron sin anexos.

El SIMEV publica muchos diciembres como informe "Intermedios" y no como "Cierre"; desde el 24-nov-2026
el conector prueba los dos, pero los diciembres que ya se revisaron sin anexos quedaron marcados como
vistos en ``meta/estado_fuentes/sfc_rnve_niif.json`` y no se vuelven a pedir. Este script quita esas
marcas (solo diciembres con 0 anexos) para que la próxima corrida de ``invest ingest sfc_rnve_niif`` los
pida otra vez. Es idempotente: correrlo dos veces no cambia nada la segunda.

Con ``--emisor CO-XXX`` devuelve a la cola todos los cortes sin anexos de ese emisor (no solo los
diciembres): sirve cuando se corrigen sus códigos del SIMEV (Bolsa Mercantil, 25-nov-2026) o para
volver a pedir trimestres que el SIMEV no tenía (Corficolombiana mar y jun-2025).

Uso (desde invest-pipeline):  uv run python scripts/reabrir_diciembres_niif.py ..\\invest-data
                              uv run python scripts/reabrir_diciembres_niif.py ..\\invest-data --emisor CO-COR02
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def reabrir(carpeta_datos: Path, emisor: str | None = None) -> int:
    ruta = carpeta_datos / "meta" / "estado_fuentes" / "sfc_rnve_niif.json"
    estado = json.loads(ruta.read_text(encoding="utf-8"))
    informes = estado.get("informes", {})
    quitar = [k for k, v in informes.items()
              if isinstance(v, dict) and not v.get("anexos")
              and (k.startswith(emisor + "|") if emisor else k.endswith("-12-31"))]
    for k in quitar:
        del informes[k]
    if quitar:
        ruta.write_text(json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return len(quitar)


if __name__ == "__main__":
    args = sys.argv[1:]
    emisor = None
    if "--emisor" in args:
        i = args.index("--emisor")
        emisor = args[i + 1]
        del args[i:i + 2]
    carpeta = Path(args[0]) if args else Path("..") / "invest-data"
    n = reabrir(carpeta, emisor)
    print(f"Cortes sin anexos devueltos a la cola{' de ' + emisor if emisor else ' (diciembres)'}: {n}")
