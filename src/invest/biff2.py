"""Lector mínimo de hojas de cálculo Excel antiguas (BIFF2 a BIFF4, una sola hoja).

La Superintendencia Financiera publica las series diarias de cada acción como .xls en formato
BIFF2 (Excel 2.x, 1987). Pandas y xlrd ya no leen ese formato de forma fiable, así que este módulo
recorre los registros directamente. Cada registro es: id (u16), longitud (u16), datos.

Registros que se leen (los demás se ignoran):

    BIFF2   0x0001 BLANK · 0x0002 INTEGER · 0x0003 NUMBER · 0x0004 LABEL · 0x0005 BOOLERR
    BIFF3+  0x0201 BLANK · 0x0203 NUMBER  · 0x0204 LABEL  · 0x027E RK     · 0x0205 BOOLERR
    0x000A  EOF (fin de la hoja)

Formato BIFF2 de una celda: fila (u16), columna (u16), 3 bytes de atributos y luego el valor.
Si la SFC cambiara a un .xls moderno (contenedor OLE2, firma D0 CF 11 E0), se usa xlrd si está
instalado; si no, se pide instalarlo con un mensaje claro.
"""

from __future__ import annotations

import struct

FIRMA_OLE2 = b"\xd0\xcf\x11\xe0"
CODIFICACION = "cp1252"


class FormatoNoSoportado(ValueError):
    pass


def _rk(valor: int) -> float:
    """Decodifica un número RK (BIFF3+): entero o double truncado, opcionalmente dividido por 100."""
    if valor & 0x02:
        n = float(valor >> 2 if valor < 0x80000000 else (valor >> 2) - (1 << 30))
    else:
        n = struct.unpack("<d", struct.pack("<Q", (valor & 0xFFFFFFFC) << 32))[0]
    return n / 100 if valor & 0x01 else n


def _leer_ole2(contenido: bytes) -> list[list]:
    try:
        import xlrd  # type: ignore
    except ImportError as e:  # pragma: no cover - depende del entorno
        raise FormatoNoSoportado(
            "El archivo es un .xls moderno (OLE2). Instale xlrd: uv add xlrd"
        ) from e
    hoja = xlrd.open_workbook(file_contents=contenido).sheet_by_index(0)  # pragma: no cover
    return [hoja.row_values(i) for i in range(hoja.nrows)]  # pragma: no cover


def leer_xls(contenido: bytes) -> list[list]:
    """Devuelve la primera hoja como lista de filas; las celdas vacías son None."""
    if contenido[:4] == FIRMA_OLE2:
        return _leer_ole2(contenido)
    if len(contenido) < 4 or struct.unpack_from("<H", contenido, 0)[0] not in (0x0009, 0x0209, 0x0409):
        raise FormatoNoSoportado("No es un archivo BIFF (no empieza con un registro BOF)")

    celdas: dict[int, dict[int, object]] = {}

    def poner(fila: int, col: int, valor: object) -> None:
        celdas.setdefault(fila, {})[col] = valor

    p, n = 0, len(contenido)
    while p + 4 <= n:
        rid, largo = struct.unpack_from("<HH", contenido, p)
        d = p + 4
        if d + largo > n:
            break  # registro truncado: se conserva lo leído hasta aquí
        if rid == 0x000A:
            break
        if rid in (0x0002, 0x0003, 0x0004, 0x0005, 0x0001):
            fila, col = struct.unpack_from("<HH", contenido, d)
            if rid == 0x0003:
                poner(fila, col, struct.unpack_from("<d", contenido, d + 7)[0])
            elif rid == 0x0002:
                poner(fila, col, float(struct.unpack_from("<H", contenido, d + 7)[0]))
            elif rid == 0x0004:
                largo_txt = contenido[d + 7]
                poner(fila, col, contenido[d + 8 : d + 8 + largo_txt].decode(CODIFICACION, "replace"))
            elif rid == 0x0005:
                v, es_error = contenido[d + 7], contenido[d + 8]
                poner(fila, col, None if es_error else bool(v))
        elif rid in (0x0203, 0x0204, 0x027E, 0x0201, 0x0205):
            fila, col = struct.unpack_from("<HH", contenido, d)
            if rid == 0x0203:
                poner(fila, col, struct.unpack_from("<d", contenido, d + 6)[0])
            elif rid == 0x027E:
                poner(fila, col, _rk(struct.unpack_from("<I", contenido, d + 6)[0]))
            elif rid == 0x0204:
                largo_txt = struct.unpack_from("<H", contenido, d + 6)[0]
                poner(fila, col, contenido[d + 8 : d + 8 + largo_txt].decode(CODIFICACION, "replace"))
            elif rid == 0x0205:
                v, es_error = contenido[d + 6], contenido[d + 7]
                poner(fila, col, None if es_error else bool(v))
        p = d + largo

    if not celdas:
        return []
    ancho = max(max(c) for c in celdas.values()) + 1
    return [[celdas[f].get(c) for c in range(ancho)] for f in sorted(celdas)]


def leer_tabla(contenido: bytes) -> list[dict]:
    """Usa la primera fila como encabezado y devuelve una lista de diccionarios."""
    filas = leer_xls(contenido)
    if not filas:
        return []
    encabezado = [str(c).strip().lower() if c is not None else f"col{i}" for i, c in enumerate(filas[0])]
    return [dict(zip(encabezado, f)) for f in filas[1:] if any(v is not None for v in f)]
