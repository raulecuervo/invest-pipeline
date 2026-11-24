"""PDF mínimo con texto (para verificar citas con pypdf) y un cliente de modelo falso."""

from __future__ import annotations

import copy


def pdf_con_texto(lineas: list[str]) -> bytes:
    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    flujo = "BT /F1 11 Tf 50 750 Td 14 TL " + " ".join(f"({esc(l)}) '" for l in lineas) + " ET"
    flujo_b = flujo.encode("latin-1")
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(flujo_b)).encode() + b" >>\nstream\n" + flujo_b + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    salida, posiciones = b"%PDF-1.4\n", []
    for i, obj in enumerate(objetos, start=1):
        posiciones.append(len(salida))
        salida += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    salida += b"".join(f"{p:010d} 00000 n \n".encode() for p in posiciones)
    salida += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return salida


TEXTO_TERPEL = [
    "ORGANIZACION TERPEL S.A.",
    "Proyecto de distribucion de utilidades del ejercicio 2025 aprobado por la Asamblea",
    "Dividendo ordinario de $1.250 por accion pagadero en dos cuotas iguales de $625:",
    "primera cuota el 15 de abril de 2026 y segunda cuota el 15 de octubre de 2026.",
    "Periodo ex dividendo: 9 de abril de 2026 y 9 de octubre de 2026.",
]

CORRECTO = {
    "contiene_dividendos": True, "tipo_documento": "aprobado", "ejercicio": 2025, "fecha_asamblea": "2026-03-18",
    "decretos": [{
        "clase_accion": "ordinaria", "tipo": "ordinario", "forma": "efectivo", "moneda": "COP",
        "valor_por_accion": 1250.0,
        "cuotas": [
            {"numero": 1, "valor": 625.0, "fecha_ex_dividendo": "2026-04-09", "fecha_pago": "2026-04-15"},
            {"numero": 2, "valor": 625.0, "fecha_ex_dividendo": "2026-10-09", "fecha_pago": "2026-10-15"},
        ],
        "cita": "Dividendo ordinario de $1.250 por accion pagadero en dos cuotas iguales de $625:",
        "pagina": 1,
    }],
    "observaciones": "",
}


def variante(**cambios) -> dict:
    """Copia de CORRECTO con cambios en el primer decreto o su primera cuota (cuota_<campo>)."""
    r = copy.deepcopy(CORRECTO)
    for k, v in cambios.items():
        if k.startswith("cuota_"):
            r["decretos"][0]["cuotas"][0][k[6:]] = v
        elif k in r:
            r[k] = v
        else:
            r["decretos"][0][k] = v
    return r


class ClienteFalso:
    """Devuelve respuestas guionizadas por modelo, en orden, y registra lo que se le pidió."""

    def __init__(self, guion: dict[str, list[dict]]):
        self.guion = {m: list(r) for m, r in guion.items()}
        self.llamadas: list[tuple[str, str]] = []

    def registrar(self, modelo, sistema, pdf, texto):
        assert pdf.startswith(b"%PDF")
        self.llamadas.append((modelo, texto))
        return copy.deepcopy(self.guion[modelo].pop(0))
