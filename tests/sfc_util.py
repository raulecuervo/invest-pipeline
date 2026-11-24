"""Utilidades de prueba para la SFC: escritor BIFF2 con la misma disposición de registros que los
archivos reales y un servidor HTTP falso."""

from __future__ import annotations

import struct
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "sfc"
ENCABEZADO = ["cd_titulo", "fc_oper", "pr_ponderado", "vr_transado"]
# Anchos de columna (registro 0x24) tal como vienen en los archivos de la SFC
ANCHOS = [0x0D00, 0x0C00, 0x0C00, 0x0B00]


def _registro(rid: int, datos: bytes) -> bytes:
    return struct.pack("<HH", rid, len(datos)) + datos


def _label(fila: int, col: int, texto: str, attr: bytes = b"\x00\x00\x00") -> bytes:
    t = texto.encode("cp1252")
    return _registro(0x0004, struct.pack("<HH", fila, col) + attr + bytes([len(t)]) + t)


def _number(fila: int, col: int, valor: float) -> bytes:
    return _registro(0x0003, struct.pack("<HH", fila, col) + b"\x00\x00\x00" + struct.pack("<d", valor))


def escribir_biff2(filas: list[tuple[str, str, float, float]]) -> bytes:
    """filas: (cd_titulo, fc_oper 'dd/mm/aaaa', pr_ponderado, vr_transado)."""
    salida = _registro(0x0009, b"\x02\x00\x10\x00")
    salida += _registro(0x0000, struct.pack("<HHHH", 0, len(filas) + 1, 0, 4))
    for col, (nombre, ancho) in enumerate(zip(ENCABEZADO, ANCHOS)):
        salida += _registro(0x0024, struct.pack("<BBH", col, col, ancho))
        salida += _label(0, col, nombre, attr=b"\x00\x00\x7a")
    for i, (titulo, fecha, precio, valor) in enumerate(filas, start=1):
        salida += _label(i, 0, titulo) + _label(i, 1, fecha) + _number(i, 2, precio) + _number(i, 3, valor)
    return salida + _registro(0x000A, b"")


# Filas reales de Ecopetrol (coc04paao008), tomadas de los archivos de la SFC el 22-nov-2026
ECOPETROL_2026 = [
    ("COC04PAAO008", "02/01/2026", 1900.4370452950802, 32808253845.0),
    ("COC04PAAO008", "05/01/2026", 1953.84327928408, 61369959495.0),
    ("COC04PAAO008", "06/01/2026", 2077.50086312169, 100617016970.0),
    ("COC04PAAO008", "20/03/2026", 2768.6581611565803, 290892405935.0),
    ("COC04PAAO008", "24/03/2026", 2708.2351963010105, 34176980295.0),
    ("COC04PAAO008", "24/03/2026", 2708.2351963010105, 34176980295.0),  # repetida en la fuente
    ("COC04PAAO008", "25/03/2026", 2693.2919318111904, 63364519075.0),
    ("COC04PAAO008", "13/11/2026", 2521.7904823513504, 34960121120.0),
    ("COC04PAAO008", "17/11/2026", 2494.5029333214297, 53754772105.0),
    ("COC04PAAO008", "18/11/2026", 2410.80538183601, 78350068350.0),
]
ECOPETROL_ANT = [
    ("COC04PAAO008", "26/07/2007", 1.0, 1.0),  # marcador: antes de listarse
    ("COC04PAAO008", "27/07/2007", 1.0, 1.0),
    ("COC04PAAO008", "29/12/2025", 1829.7320939360402, 9437249475.0),
    ("COC04PAAO008", "30/12/2025", 1859.20551477727, 16717949960.0),
]
CIBEST_2026 = [
    ("COTK7PAAO003", "02/01/2026", 68084.4295360236, 4708923400.0),
    ("COTK7PAAO003", "17/11/2026", 82596.5120433814, 15719107400.0),
    ("COTK7PAAO003", "18/11/2026", 79388.0416808011, 30657518220.0),
]


class ServidorFalso:
    """Reemplaza ``http.obtener``: responde por nombre de archivo y registra las solicitudes."""

    def __init__(self, archivos: dict[str, bytes], errores: dict[str, Exception] | None = None):
        self.archivos, self.errores, self.pedidos = archivos, errores or {}, []
        self.no_modificados: list[str] = []

    def __call__(self, url: str, reintentos: int = 4, timeout: int = 60) -> bytes:
        from invest.ingest import http

        nombre = url.rsplit("/", 1)[-1]
        self.pedidos.append(nombre)
        if nombre in self.errores:
            raise self.errores[nombre]
        if nombre not in self.archivos:
            raise http.NoEncontrado(f"404 en {nombre}")
        return self.archivos[nombre]

    def condicional(self, url: str, previo: dict | None = None, reintentos: int = 4, timeout: int = 60):
        """Imita la descarga condicional: ETag = huella del contenido; si no cambió, 304 (None)."""
        import hashlib

        contenido = self(url, reintentos, timeout)
        etag = '"' + hashlib.md5(contenido).hexdigest() + '"'
        if previo and previo.get("etag") == etag:
            self.no_modificados.append(url.rsplit("/", 1)[-1])
            return None, dict(previo)
        return contenido, {"etag": etag}
