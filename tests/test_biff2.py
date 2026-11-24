import struct

import pytest

from invest import biff2
from sfc_util import ECOPETROL_2026, escribir_biff2

# Primeros bytes del archivo real coc04paao008_2026.xls (215 filas), copiados en hexadecimal:
# BOF, DIMENSIONS, cuatro pares COLWIDTH + LABEL de encabezado y la primera fila de datos.
CABECERA_REAL = bytes.fromhex(
    "090004000200100000000800" "0000d70000000400"
    "240004000000000d" "040011000000000000007a0963645f746974756c6f"
    "240004000101000c" "04000f000000010000007a0766635f6f706572"
    "240004000202000c" "040014000000020000007a0c70725f706f6e64657261646f"
    "240004000303000b" "040013000000030000007a0b76725f7472616e7361646f"
    "0400140001000000000000" "0c434f4330345041414f303038"
    "0400120001000100000000" "0a30322f30312f32303236"
    "03000f00010002000000" "00f644cd88bfb19d40"
    "03000f00010003000000" "00000054e6188e1e42"
)


def test_el_escritor_de_prueba_reproduce_el_archivo_real():
    # El archivo real tiene encabezado + 214 filas, por eso DIMENSIONS dice 215 (0xd7)
    filas = [ECOPETROL_2026[0]] + [("X", "01/01/2026", 1.0, 1.0)] * 213
    assert escribir_biff2(filas)[: len(CABECERA_REAL)] == CABECERA_REAL


def test_lee_encabezado_y_valores():
    tabla = biff2.leer_tabla(escribir_biff2(ECOPETROL_2026))
    assert len(tabla) == len(ECOPETROL_2026)
    assert tabla[0] == {
        "cd_titulo": "COC04PAAO008",
        "fc_oper": "02/01/2026",
        "pr_ponderado": 1900.4370452950802,
        "vr_transado": 32808253845.0,
    }
    assert tabla[-1]["fc_oper"] == "18/11/2026"


def test_lee_registros_biff3_y_numeros_rk():
    def reg(rid, datos):
        return struct.pack("<HH", rid, len(datos)) + datos

    texto = "Año".encode("cp1252")
    contenido = (
        reg(0x0209, b"\x00\x00\x10\x00")
        + reg(0x0204, struct.pack("<HHHH", 0, 0, 0, len(texto)) + texto)
        + reg(0x027E, struct.pack("<HHHI", 1, 0, 0, (1234 << 2) | 0x02 | 0x01))  # 12.34 como RK
        + reg(0x0203, struct.pack("<HHH", 1, 1, 0) + struct.pack("<d", 2.5))
        + reg(0x000A, b"")
    )
    assert biff2.leer_xls(contenido) == [["Año", None], [12.34, 2.5]]


def test_archivo_vacio_o_ajeno():
    assert biff2.leer_tabla(escribir_biff2([])) == []
    with pytest.raises(biff2.FormatoNoSoportado):
        biff2.leer_xls(b"<html>no es excel</html>")


def test_xls_moderno_pide_xlrd_si_no_esta():
    try:
        import xlrd  # noqa: F401
    except ImportError:
        with pytest.raises(biff2.FormatoNoSoportado, match="xlrd"):
            biff2.leer_xls(biff2.FIRMA_OLE2 + b"\x00" * 100)
    else:  # pragma: no cover
        pytest.skip("xlrd instalado")
