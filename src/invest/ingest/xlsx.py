"""Lector mínimo de .xlsx con la librería estándar (zipfile + XML).

Basta para los anexos tabulares del DANE: textos compartidos, números y la primera hoja (o una
hoja por nombre). No evalúa fórmulas (usa el último valor guardado) ni lee formatos.
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"


def _columna(ref: str) -> int:
    letras = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letras:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _texto(nodo) -> str:
    return "".join(t.text or "" for t in nodo.iter(f"{{{NS['m']}}}t"))


def leer_hoja(contenido: bytes, hoja: str | None = None) -> list[list]:
    """Filas de la hoja como listas (texto o float; None si la celda está vacía)."""
    with zipfile.ZipFile(io.BytesIO(contenido)) as z:
        compartidos = []
        if "xl/sharedStrings.xml" in z.namelist():
            raiz = ET.fromstring(z.read("xl/sharedStrings.xml"))
            compartidos = [_texto(si) for si in raiz.findall("m:si", NS)]
        libro = ET.fromstring(z.read("xl/workbook.xml"))
        hojas = libro.find("m:sheets", NS).findall("m:sheet", NS)
        elegida = next((h for h in hojas if hoja is None or h.get("name") == hoja), None)
        if elegida is None:
            raise KeyError(f"La hoja {hoja!r} no está en el libro")
        rid = elegida.get(f"{{{NS['r']}}}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        destino = next(r.get("Target") for r in rels.iter(_REL) if r.get("Id") == rid)
        ruta = destino.lstrip("/") if destino.startswith("/") else "xl/" + destino
        datos = ET.fromstring(z.read(ruta))
    filas: list[list] = []
    for fila in datos.iter(f"{{{NS['m']}}}row"):
        valores: dict[int, object] = {}
        for c in fila.findall("m:c", NS):
            tipo, v = c.get("t"), c.find("m:v", NS)
            if tipo == "s" and v is not None:
                valor = compartidos[int(v.text)]
            elif tipo == "inlineStr":
                valor = _texto(c)
            elif v is None or v.text is None:
                continue
            elif tipo in ("str", "e"):
                valor = v.text
            else:
                try:
                    valor = float(v.text)
                except ValueError:
                    valor = v.text
            valores[_columna(c.get("r"))] = valor
        if valores:
            filas.append([valores.get(i) for i in range(max(valores) + 1)])
        else:
            filas.append([])
    return filas
