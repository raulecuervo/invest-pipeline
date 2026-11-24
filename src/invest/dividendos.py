"""Dividendos: extracción desde los PDF con dos agentes, consenso y revisión humana.

Flujo de ``invest dividendos``:

1. Toma de gold/fact_evento_corporativo los eventos de dividendos (categorías ``dividendo`` y
   ``ex_dividendo``) con PDF guardado y emisor con acciones inscritas, que aún no se hayan procesado
   (o cuyo PDF cambió).
2. **Lectura independiente.** El agente A (analista) y el agente B (auditor) leen el PDF original,
   con modelos e instrucciones distintos, y devuelven la misma estructura: tipo de documento,
   ejercicio, y por cada decreto (clase de acción, ordinario/extraordinario, efectivo/acciones) el
   valor por acción y sus cuotas con fecha ex-dividendo y fecha de pago, más una cita literal.
3. **Verificación.** Cada cita se busca en el texto del PDF; una cita que no aparece cuenta como
   diferencia (protege contra valores inventados).
4. **Revisión cruzada.** Si hay diferencias, cada agente recibe su respuesta, la del otro y la lista
   de diferencias, vuelve a leer el PDF y corrige o sostiene su lectura. Hasta ``rondas_maximas``.
5. **Resultado.** ``consenso`` (coinciden en todo lo comparado), ``sin_dividendo`` (ambos dicen que
   el documento no trae dividendos) o ``en_disputa`` (no se pusieron de acuerdo).
6. **Revisión humana.** invest-data/revision/dividendos.csv lista cada cuota con el estado, las dos
   propuestas y las diferencias. La columna ``revision`` (aprobado, corregido, rechazado) manda: lo
   que se marca ahí no se sobrescribe en corridas siguientes.
7. **gold/fact_dividendo.** Entra lo revisado y lo que tuvo consenso; lo que está en disputa sin
   revisar o rechazado queda fuera. Varios documentos del mismo dividendo (proyecto, aprobación,
   fechas ex-dividendo) se consolidan: manda el más reciente y se completan los campos vacíos.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
import yaml

from invest import eventos, storage
from invest.config import CONFIG_DIR, data_dir

FUENTE_ID = "dividendos_dos_agentes"
TABLA = "fact_dividendo_extraccion"
CATEGORIAS = {"dividendo", "ex_dividendo"}
ARCHIVO_REVISION = "dividendos.csv"
REVISIONES_VALIDAS = {"", "aprobado", "corregido", "rechazado"}

# --------------------------------------------------------------------------- estructura pedida
ESQUEMA = {
    "type": "object",
    "properties": {
        "contiene_dividendos": {
            "type": "boolean",
            "description": "true si el documento fija montos o fechas de un dividendo de acciones",
        },
        "tipo_documento": {
            "type": "string",
            "enum": ["proyecto", "aprobado", "fechas_ex_dividendo", "confirmacion_pago", "otro"],
            "description": "proyecto = propuesta a presentar a la asamblea; aprobado = decretado por la asamblea",
        },
        "ejercicio": {"type": ["integer", "null"], "description": "año de las utilidades que se distribuyen"},
        "fecha_asamblea": {"type": ["string", "null"], "description": "AAAA-MM-DD"},
        "decretos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clase_accion": {"type": "string", "enum": ["ordinaria", "preferencial", "todas", "otra"]},
                    "tipo": {"type": "string", "enum": ["ordinario", "extraordinario"]},
                    "forma": {"type": "string", "enum": ["efectivo", "acciones"]},
                    "moneda": {"type": "string", "description": "COP, USD..."},
                    "valor_por_accion": {"type": ["number", "null"], "description": "valor anual total por acción"},
                    "cuotas": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "numero": {"type": "integer"},
                                "valor": {"type": ["number", "null"]},
                                "fecha_ex_dividendo": {"type": ["string", "null"], "description": "AAAA-MM-DD"},
                                "fecha_pago": {"type": ["string", "null"], "description": "AAAA-MM-DD"},
                            },
                            "required": ["numero", "valor", "fecha_ex_dividendo", "fecha_pago"],
                        },
                    },
                    "cita": {"type": "string", "description": "frase copiada literalmente del documento que sustenta el valor"},
                    "pagina": {"type": ["integer", "null"]},
                },
                "required": ["clase_accion", "tipo", "forma", "moneda", "valor_por_accion", "cuotas", "cita"],
            },
        },
        "observaciones": {"type": "string"},
    },
    "required": ["contiene_dividendos", "tipo_documento", "ejercicio", "decretos", "observaciones"],
}
HERRAMIENTA = {
    "name": "registrar_dividendos",
    "description": "Registra los dividendos que establece el documento, tal como están escritos.",
    "input_schema": ESQUEMA,
}

REGLAS_COMUNES = """Reglas:
- Usa solo lo que está escrito en el documento. No calcules ni supongas fechas: si una fecha
  ex-dividendo o de pago no aparece, déjala en null.
- Valores numéricos por acción, en la moneda del documento, sin separadores de miles
  (\"$1.250,50\" se registra 1250.5).
- Un decreto por combinación de clase de acción, tipo (ordinario o extraordinario) y forma
  (efectivo o acciones). Si se paga en cuotas, una entrada por cuota con su valor y fechas; si es un
  solo pago, una cuota número 1.
- La cita debe ser una frase copiada exactamente del documento, la que muestra el valor por acción.
- Si el documento no fija montos ni fechas de dividendos (por ejemplo, solo confirma que se pagó),
  marca contiene_dividendos = false y explica en observaciones.
Registra el resultado con la herramienta registrar_dividendos."""

INSTRUCCIONES = {
    "a": (
        "Eres un analista financiero que lee documentos de emisores colombianos publicados en la "
        "Superintendencia Financiera. Extrae los dividendos que el documento establece.\n" + REGLAS_COMUNES
    ),
    "b": (
        "Eres un auditor que verifica documentos de distribución de utilidades. Antes de registrar, "
        "recorre el documento completo de principio a fin, ubica cada tabla y párrafo con montos por "
        "acción y fechas, y comprueba que la suma de las cuotas coincida con el valor por acción. "
        "Distingue con cuidado entre lo que se propone a la asamblea y lo que ya fue aprobado.\n"
        + REGLAS_COMUNES
    ),
}


def config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "extraccion.yaml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- cliente del modelo
class ClienteLLM:
    """Llama a la API de Claude con el PDF como documento y pide la respuesta estructurada.

    Primero obliga a usar la herramienta (``tool_choice`` = la herramienta). Algunos modelos no
    aceptan esa opción; en ese caso se pasa a ``tool_choice`` automático con la instrucción
    explícita de registrar con la herramienta, y si el modelo responde sin usarla se le pide una
    vez más en la misma conversación.
    """

    AVISO = ("\n\nResponde únicamente llamando la herramienta "
             f"{HERRAMIENTA['name']} con el registro completo; no escribas texto fuera de ella.")

    def __init__(self, max_tokens: int = 4000, cliente=None):
        if cliente is None:
            import anthropic

            cliente = anthropic.Anthropic()
        self._cliente = cliente
        self.max_tokens = max_tokens
        self._forzar = True

    @staticmethod
    def _registro(respuesta) -> dict | None:
        for bloque in respuesta.content:
            if getattr(bloque, "type", "") == "tool_use":
                return dict(bloque.input)
        return None

    def _crear(self, modelo: str, sistema: str, mensajes: list, forzar: bool):
        return self._cliente.messages.create(
            model=modelo, max_tokens=self.max_tokens, system=sistema if forzar else sistema + self.AVISO,
            tools=[HERRAMIENTA],
            tool_choice={"type": "tool", "name": HERRAMIENTA["name"]} if forzar else {"type": "auto"},
            messages=mensajes,
        )

    def registrar(self, modelo: str, sistema: str, pdf: bytes, texto: str) -> dict:
        mensajes = [{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf).decode("ascii")}},
                {"type": "text", "text": texto},
            ],
        }]
        respuesta = None
        if self._forzar:
            try:
                respuesta = self._crear(modelo, sistema, mensajes, forzar=True)
            except Exception as e:  # el modelo no acepta tool_choice forzado: se sigue sin forzar
                if "tool_choice" not in str(e):
                    raise
                self._forzar = False
        if respuesta is None:
            respuesta = self._crear(modelo, sistema, mensajes, forzar=False)
        registro = self._registro(respuesta)
        if registro is None and not self._forzar:
            mensajes += [{"role": "assistant", "content": respuesta.content},
                         {"role": "user", "content": f"Registra el resultado llamando la herramienta {HERRAMIENTA['name']}."}]
            registro = self._registro(self._crear(modelo, sistema, mensajes, forzar=False))
        if registro is None:
            raise RuntimeError("El modelo no devolvió el registro estructurado")
        return registro


@dataclass
class Agente:
    clave: str
    nombre: str
    modelo: str
    cliente: object

    def extraer(self, pdf: bytes, contexto: str) -> dict:
        texto = f"Contexto del evento publicado en la SFC:\n{contexto}\n\nExtrae los dividendos del documento adjunto."
        return self.cliente.registrar(self.modelo, INSTRUCCIONES[self.clave], pdf, texto)

    def revisar(self, pdf: bytes, contexto: str, propio: dict, otro: dict, diferencias: list[str]) -> dict:
        texto = (
            f"Contexto del evento publicado en la SFC:\n{contexto}\n\n"
            "Otro revisor leyó el mismo documento y llegó a un resultado distinto.\n\n"
            f"Tu registro anterior:\n{json.dumps(propio, ensure_ascii=False, indent=1)}\n\n"
            f"Registro del otro revisor:\n{json.dumps(otro, ensure_ascii=False, indent=1)}\n\n"
            "Diferencias encontradas:\n- " + "\n- ".join(diferencias) + "\n\n"
            "Vuelve a leer el documento y resuelve cada diferencia según lo que está escrito. Corrige "
            "si el otro tiene razón; sostén tu valor si el documento te respalda (no cambies solo para "
            "coincidir). Registra de nuevo el resultado completo."
        )
        return self.cliente.registrar(self.modelo, INSTRUCCIONES[self.clave], pdf, texto)


# --------------------------------------------------------------------------- comparación
def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _decretos(resultado: dict) -> dict[tuple, dict]:
    salida = {}
    for d in resultado.get("decretos") or []:
        clave = (d.get("clase_accion"), d.get("tipo"), d.get("forma"))
        cuotas = {int(c.get("numero") or 1): c for c in d.get("cuotas") or []}
        salida[clave] = {**d, "cuotas": cuotas}
    return salida


def _expandir_todas(da: dict, db: dict) -> dict:
    """Si ``da`` registra un decreto para "todas" las clases y ``db`` lo separa por clase (mismo tipo
    y forma, sin "todas"), copia el de ``da`` a cada clase de ``db`` para compararlos uno a uno."""
    salida = dict(da)
    for (clase, tipo, forma), d in da.items():
        if clase != "todas" or (clase, tipo, forma) in db:
            continue
        clases = [c for (c, t, f) in db if (t, f) == (tipo, forma)]
        if clases:
            del salida[(clase, tipo, forma)]
            for c in clases:
                salida[(c, tipo, forma)] = {**d, "clase_accion": c}
    return salida


def comparar(a: dict, b: dict, tolerancia: float = 0.01) -> list[str]:
    """Diferencias entre dos registros en lo que importa para los cálculos.

    "todas" las clases equivale a registrar la misma cifra por clase. Si ninguno de los dos encontró
    dividendos, el tipo de documento y el ejercicio no importan.
    """
    dif: list[str] = []
    sin_dividendo = not a.get("decretos") and not b.get("decretos") and not a.get("contiene_dividendos") \
        and not b.get("contiene_dividendos")
    for campo in ("contiene_dividendos",) + (() if sin_dividendo else ("tipo_documento", "ejercicio")):
        if a.get(campo) != b.get(campo):
            dif.append(f"{campo}: A={a.get(campo)!r} B={b.get(campo)!r}")
    da, db = _decretos(a), _decretos(b)
    da, db = _expandir_todas(da, db), _expandir_todas(db, da)
    for clave in sorted(set(da) | set(db), key=str):
        nombre = "/".join(str(x) for x in clave)
        if clave not in da or clave not in db:
            dif.append(f"decreto {nombre}: solo lo registra {'A' if clave in da else 'B'}")
            continue
        x, y = da[clave], db[clave]
        if (x.get("moneda") or "").upper() != (y.get("moneda") or "").upper():
            dif.append(f"{nombre} moneda: A={x.get('moneda')} B={y.get('moneda')}")
        va, vb = _num(x.get("valor_por_accion")), _num(y.get("valor_por_accion"))
        if (va is None) != (vb is None) or (va is not None and abs(va - vb) > tolerancia):
            dif.append(f"{nombre} valor por acción: A={va} B={vb}")
        for n in sorted(set(x["cuotas"]) | set(y["cuotas"])):
            if n not in x["cuotas"] or n not in y["cuotas"]:
                dif.append(f"{nombre} cuota {n}: solo la registra {'A' if n in x['cuotas'] else 'B'}")
                continue
            ca, cb = x["cuotas"][n], y["cuotas"][n]
            wa, wb = _num(ca.get("valor")), _num(cb.get("valor"))
            if (wa is None) != (wb is None) or (wa is not None and abs(wa - wb) > tolerancia):
                dif.append(f"{nombre} cuota {n} valor: A={wa} B={wb}")
            for f in ("fecha_ex_dividendo", "fecha_pago"):
                if (ca.get(f) or None) != (cb.get(f) or None):
                    dif.append(f"{nombre} cuota {n} {f}: A={ca.get(f)} B={cb.get(f)}")
    return dif


def texto_pdf(pdf: bytes) -> str:
    try:
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)
    except Exception:
        return ""


def _compacto(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", "", s)


def verificar_citas(resultado: dict, texto: str, quien: str) -> list[str]:
    """Citas que no aparecen en el texto del PDF. Sin texto extraíble (PDF escaneado) no se verifica."""
    if len(_compacto(texto)) < 50:
        return []
    compacto = _compacto(texto)
    problemas = []
    for d in resultado.get("decretos") or []:
        cita = _compacto(d.get("cita", ""))
        if cita and cita not in compacto:
            nombre = f"{d.get('clase_accion')}/{d.get('tipo')}/{d.get('forma')}"
            problemas.append(f"{nombre}: la cita de {quien} no aparece en el PDF")
    return problemas


def alertas(resultado: dict, tolerancia: float = 0.01) -> list[str]:
    """Inconsistencias internas que no impiden el consenso pero conviene mirar."""
    salida = []
    for d in resultado.get("decretos") or []:
        total = _num(d.get("valor_por_accion"))
        valores = [_num(c.get("valor")) for c in d.get("cuotas") or []]
        if total is not None and valores and all(v is not None for v in valores):
            if abs(sum(valores) - total) > tolerancia * max(1, len(valores)):
                salida.append(f"{d.get('clase_accion')}: la suma de las cuotas ({sum(valores)}) no da el valor por acción ({total})")
        for c in d.get("cuotas") or []:
            ex, pago = c.get("fecha_ex_dividendo"), c.get("fecha_pago")
            if ex and pago and ex > pago:
                salida.append(f"{d.get('clase_accion')} cuota {c.get('numero')}: fecha ex-dividendo posterior al pago")
    return salida


@dataclass
class Resultado:
    estado: str
    final: dict | None
    a: dict
    b: dict
    rondas: int
    diferencias: list[str] = field(default_factory=list)
    alertas: list[str] = field(default_factory=list)


def consensuar(pdf: bytes, contexto: str, agente_a: Agente, agente_b: Agente,
               rondas_maximas: int = 2, tolerancia: float = 0.01) -> Resultado:
    texto = texto_pdf(pdf)

    def diferencias(x: dict, y: dict) -> list[str]:
        return juzgar(x, y, texto, tolerancia)[0]

    a = agente_a.extraer(pdf, contexto)
    b = agente_b.extraer(pdf, contexto)
    dif = diferencias(a, b)
    ronda = 0
    while dif and ronda < rondas_maximas:
        ronda += 1
        a, b = (agente_a.revisar(pdf, contexto, a, b, dif), agente_b.revisar(pdf, contexto, b, a, dif))
        dif = diferencias(a, b)
    return _resultado(a, b, ronda, texto, tolerancia)


def juzgar(a: dict, b: dict, texto: str, tolerancia: float = 0.01) -> tuple[list[str], list[str]]:
    """(diferencias que impiden el consenso, avisos).

    Una cita que no aparece en el texto del PDF solo cuenta como diferencia si los dos agentes no
    coinciden en las cifras: muchos PDF de la SFC traen el cuerpo como imagen y solo el encabezado
    como texto, así que la cita no se puede encontrar aunque esté bien. Si dos modelos distintos
    leyeron por separado las mismas cifras, la cita faltante queda como aviso.
    """
    dif = comparar(a, b, tolerancia)
    citas = verificar_citas(a, texto, "A") + verificar_citas(b, texto, "B")
    if dif or not citas:
        return dif + citas, []
    compacto = _compacto(texto)
    visibles = [d for d in a.get("decretos") or [] if _cifra_en_texto(d.get("valor_por_accion"), compacto)]
    if visibles:  # la cifra sí está en el texto y la cita no: posible cita inventada, se revisa
        return citas, []
    return [], [c + " (los dos coinciden en las cifras; esa parte del PDF es imagen)" for c in citas]


def _cifra_en_texto(valor, compacto: str) -> bool:
    """¿Aparece la cifra en el texto, con separadores colombianos o estadounidenses?"""
    v = _num(valor)
    if v is None:
        return False
    entero, dec = f"{v:.2f}".split(".")
    miles = f"{int(entero):,}"
    variantes = {miles.replace(",", "."), miles, entero}
    formas = set()
    for m in variantes:
        formas.add(m)
        if dec != "00":
            formas |= {m.replace(".", "") + "," + dec, m + "," + dec, m + "," + dec.rstrip("0"), m + "." + dec}
    return any(f in compacto for f in formas if len(f) >= 2)


def _resultado(a: dict, b: dict, ronda: int, texto: str, tolerancia: float) -> "Resultado":
    dif, avisos = juzgar(a, b, texto, tolerancia)
    if dif:
        return Resultado("en_disputa", None, a, b, ronda, dif, alertas(a, tolerancia) + alertas(b, tolerancia))
    estado = "consenso" if a.get("contiene_dividendos") or a.get("decretos") else "sin_dividendo"
    return Resultado(estado, a, a, b, ronda, [], avisos + alertas(a, tolerancia))


def reevaluar() -> dict:
    """Vuelve a juzgar los documentos en disputa con las lecturas ya guardadas (sin llamar a la API).

    Sirve cuando cambian las reglas de comparación. Devuelve conteos.
    """
    cfg = config()
    tabla = storage.leer_tabla("silver", TABLA)
    conteo = {"en_disputa_antes": 0, "consenso": 0, "sin_dividendo": 0, "en_disputa": 0}
    if tabla is None or tabla.is_empty():
        return conteo
    filas = []
    for r in tabla.filter(pl.col("estado") == "en_disputa").iter_rows(named=True):
        conteo["en_disputa_antes"] += 1
        a, b = json.loads(r["json_a"] or "{}"), json.loads(r["json_b"] or "{}")
        ruta = storage.data_dir() / "anexos" / r["anexo_archivo"]
        texto = texto_pdf(ruta.read_bytes()) if ruta.exists() else ""
        res = _resultado(a, b, r["rondas"] or 0, texto, cfg.get("tolerancia_valor", 0.01))
        conteo[res.estado] += 1
        filas.append({**r, "estado": res.estado, "diferencias": "\n".join(res.diferencias),
                      "alertas": "\n".join(res.alertas),
                      "json_final": json.dumps(res.final, ensure_ascii=False) if res.final else None})
    if filas:
        storage.upsert_por_anio("silver", TABLA, pl.DataFrame(filas, schema=ESQUEMA_SILVER), ("evento_id",), "fecha")
    exportar()
    return conteo


# --------------------------------------------------------------------------- proceso completo
def _contexto(evento: dict) -> str:
    return (f"Entidad: {evento['entidad']}\nFecha de publicación: {evento['fecha']}\n"
            f"Tema: {evento['tema']}\nResumen: {evento['resumen']}")


def pendientes(reprocesar: bool = False) -> list[dict]:
    gold = eventos.construir_gold()
    if gold is None:
        return []
    candidatos = gold.filter(
        pl.col("categoria").is_in(list(CATEGORIAS))
        & pl.col("anexo_archivo").is_not_null()
        & pl.col("emisor_id").is_not_null()
    ).sort("fecha")
    hechos = storage.leer_tabla("silver", TABLA)
    previos = {} if hechos is None or reprocesar else {
        e: (h, s) for e, h, s in hechos.select("evento_id", "huella_pdf", "estado").iter_rows()
    }
    salida = []
    for ev in candidatos.iter_rows(named=True):
        ruta = data_dir() / "anexos" / ev["anexo_archivo"]
        if not ruta.exists() or ruta.suffix.lower() != ".pdf":
            continue
        huella = hashlib.sha256(ruta.read_bytes()).hexdigest()
        previo = previos.get(ev["evento_id"])
        if previo and previo[0] == huella and previo[1] != "error":
            continue
        salida.append({**ev, "ruta": ruta, "huella_pdf": huella})
    return salida


ESQUEMA_SILVER = {
    "evento_id": pl.Utf8, "fecha": pl.Date, "emisor_id": pl.Utf8, "entidad": pl.Utf8,
    "anexo_archivo": pl.Utf8, "huella_pdf": pl.Utf8, "estado": pl.Utf8, "rondas": pl.Int64,
    "diferencias": pl.Utf8, "alertas": pl.Utf8, "json_a": pl.Utf8, "json_b": pl.Utf8, "json_final": pl.Utf8,
    "modelo_a": pl.Utf8, "modelo_b": pl.Utf8, "procesado_ts": pl.Datetime("us", "UTC"),
}


def procesar(cliente=None, maximo: int | None = None, reprocesar: bool = False) -> dict:
    """Extrae los PDF pendientes. Devuelve conteos (nunca valores: los registros de Actions son públicos)."""
    cfg = config()
    lista = pendientes(reprocesar)
    conteo = {"pendientes": len(lista), "procesados": 0, "consenso": 0, "sin_dividendo": 0,
              "en_disputa": 0, "error": 0}
    if lista and cliente is None and not os.environ.get("ANTHROPIC_API_KEY"):
        conteo["omitido"] = "falta ANTHROPIC_API_KEY"
        exportar()
        return conteo
    if lista and cliente is None:
        cliente = ClienteLLM(cfg.get("max_tokens", 4000))
    ag = cfg["agentes"]
    agente_a = Agente("a", ag["a"]["nombre"], ag["a"]["modelo"], cliente)
    agente_b = Agente("b", ag["b"]["nombre"], ag["b"]["modelo"], cliente)
    limite_bytes = cfg.get("tamano_maximo_mb", 25) * 1024 * 1024
    filas = []
    for ev in lista[: maximo or cfg.get("max_documentos_por_corrida", 25)]:
        pdf = ev["ruta"].read_bytes()
        base = {k: ev[k] for k in ("evento_id", "fecha", "emisor_id", "entidad", "anexo_archivo", "huella_pdf")}
        base.update(modelo_a=agente_a.modelo, modelo_b=agente_b.modelo, procesado_ts=datetime.now(timezone.utc))
        try:
            if len(pdf) > limite_bytes:
                raise ValueError("PDF más grande que el límite configurado")
            r = consensuar(pdf, _contexto(ev), agente_a, agente_b, cfg.get("rondas_maximas", 2),
                           cfg.get("tolerancia_valor", 0.01))
            filas.append({**base, "estado": r.estado, "rondas": r.rondas,
                          "diferencias": "\n".join(r.diferencias), "alertas": "\n".join(r.alertas),
                          "json_a": json.dumps(r.a, ensure_ascii=False), "json_b": json.dumps(r.b, ensure_ascii=False),
                          "json_final": json.dumps(r.final, ensure_ascii=False) if r.final else None})
            conteo[r.estado] += 1
        except Exception as e:
            filas.append({**base, "estado": "error", "rondas": 0, "diferencias": f"{type(e).__name__}: {e}",
                          "alertas": "", "json_a": None, "json_b": None, "json_final": None})
            conteo["error"] += 1
        conteo["procesados"] += 1
    if filas:
        storage.upsert_por_anio("silver", TABLA, pl.DataFrame(filas, schema=ESQUEMA_SILVER), ("evento_id",), "fecha")
    exportar()
    return conteo


# --------------------------------------------------------------------------- revisión humana
COLUMNAS_REVISION = [
    "evento_id", "emisor_id", "entidad", "fecha_evento", "documento", "estado_extraccion", "tipo_documento",
    "ejercicio", "clase_accion", "tipo", "forma", "moneda", "valor_por_accion", "cuota", "valor_cuota",
    "fecha_ex_dividendo", "fecha_pago", "diferencias", "alertas", "propuesta_agente_a", "propuesta_agente_b",
    "revision", "notas_revision",
]
CLAVE_FILA = ["evento_id", "clase_accion", "tipo", "forma", "cuota"]


def _decimal(v) -> str:
    return "" if v is None else (f"{float(v):.6f}".rstrip("0").rstrip(".")).replace(".", ",")


def _resumen_cuota(d: dict | None, c: dict | None) -> str:
    if not d:
        return "no lo registra"
    partes = [f"valor/acción {d.get('valor_por_accion')} {d.get('moneda') or ''}".strip()]
    if c:
        partes.append(f"cuota {c.get('numero')}: {c.get('valor')}, ex {c.get('fecha_ex_dividendo')}, pago {c.get('fecha_pago')}")
    else:
        partes.append("sin esta cuota")
    return " | ".join(partes)


def _filas_revision(reg: dict) -> list[dict]:
    a = json.loads(reg["json_a"]) if reg["json_a"] else {}
    b = json.loads(reg["json_b"]) if reg["json_b"] else {}
    base_doc = reg["json_final"] and json.loads(reg["json_final"]) or a or b
    da, db = _decretos(a), _decretos(b)
    comunes = {
        "evento_id": reg["evento_id"], "emisor_id": reg["emisor_id"], "entidad": reg["entidad"],
        "fecha_evento": reg["fecha"].isoformat(), "documento": reg["anexo_archivo"],
        "estado_extraccion": reg["estado"], "tipo_documento": base_doc.get("tipo_documento") or "",
        "ejercicio": str(base_doc.get("ejercicio") or ""), "diferencias": reg["diferencias"] or "",
        "alertas": reg["alertas"] or "", "revision": "", "notas_revision": "",
    }
    if reg["estado"] in ("sin_dividendo", "error") or not (set(da) | set(db)):
        return [{**comunes, "clase_accion": "", "tipo": "", "forma": "", "moneda": "", "valor_por_accion": "",
                 "cuota": "", "valor_cuota": "", "fecha_ex_dividendo": "", "fecha_pago": "",
                 "propuesta_agente_a": (a.get("observaciones") or "")[:300],
                 "propuesta_agente_b": (b.get("observaciones") or "")[:300]}]
    filas = []
    for clave in sorted(set(da) | set(db), key=str):
        x, y = da.get(clave), db.get(clave)
        principal = x or y
        for n in sorted(set((x or {}).get("cuotas", {})) | set((y or {}).get("cuotas", {}))):
            c = (principal["cuotas"].get(n) or (y or x)["cuotas"].get(n) or {})
            filas.append({
                **comunes, "clase_accion": clave[0], "tipo": clave[1], "forma": clave[2],
                "moneda": principal.get("moneda") or "", "valor_por_accion": _decimal(principal.get("valor_por_accion")),
                "cuota": str(n), "valor_cuota": _decimal(c.get("valor")),
                "fecha_ex_dividendo": c.get("fecha_ex_dividendo") or "", "fecha_pago": c.get("fecha_pago") or "",
                "propuesta_agente_a": _resumen_cuota(x, (x or {}).get("cuotas", {}).get(n)),
                "propuesta_agente_b": _resumen_cuota(y, (y or {}).get("cuotas", {}).get(n)),
            })
    return filas


def ruta_revision() -> Path:
    carpeta = data_dir() / "revision"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / ARCHIVO_REVISION


def leer_revision() -> pl.DataFrame | None:
    ruta = ruta_revision()
    if not ruta.exists():
        return None
    with open(ruta, encoding="utf-8-sig", errors="replace", newline="") as f:
        filas = list(csv.reader(f, delimiter=";"))
    if not filas:
        return None
    cabecera = [c.strip() for c in filas[0]]
    datos = [dict(zip(cabecera, f + [""] * (len(cabecera) - len(f)))) for f in filas[1:] if any(f)]
    df = pl.DataFrame(datos, schema={c: pl.Utf8 for c in cabecera}).fill_null("")
    for c in COLUMNAS_REVISION:
        if c not in df.columns:
            df = df.with_columns(pl.lit("").alias(c))
    return df.select(COLUMNAS_REVISION).with_columns(pl.col("revision").str.strip_chars().str.to_lowercase())


def exportar() -> Path | None:
    """Regenera el archivo de revisión sin tocar las filas que ya tienen una decisión humana."""
    hechos = storage.leer_tabla("silver", TABLA)
    if hechos is None or hechos.is_empty():
        return None
    nuevas = [f for reg in hechos.sort("fecha").iter_rows(named=True) for f in _filas_revision(reg)]
    nuevas_df = pl.DataFrame(nuevas, schema={c: pl.Utf8 for c in COLUMNAS_REVISION})
    previa = leer_revision()
    if previa is not None:
        decididas = previa.filter(pl.col("revision") != "")
        documentos_decididos = decididas.select("evento_id").unique()
        # Un documento con alguna fila decidida se conserva completo tal como lo dejó la persona
        conservar = previa.join(documentos_decididos, on="evento_id", how="semi")
        nuevas_df = pl.concat([conservar, nuevas_df.join(documentos_decididos, on="evento_id", how="anti")])
    ruta = ruta_revision()
    # csv de la biblioteca estándar: entrecomilla los campos con ";" o saltos de línea, como Excel.
    # Punto y coma como separador y BOM para que Excel en español lo abra en columnas y con tildes.
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.writer(f, delimiter=";")
        escritor.writerow(COLUMNAS_REVISION)
        escritor.writerows(nuevas_df.sort(["fecha_evento", "entidad", "clase_accion", "cuota"]).rows())
    construir_gold()
    return ruta


def _a_numero(texto: str) -> float | None:
    t = (texto or "").strip().replace("$", "").replace(" ", "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _a_fecha(texto: str) -> date | None:
    t = (texto or "").strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            pass
    return None


def revisiones_invalidas() -> list[str]:
    """Valores de la columna revision que no se reconocen (se tratan como sin revisar)."""
    rev = leer_revision()
    if rev is None:
        return []
    return sorted(set(rev.filter(~pl.col("revision").is_in(list(REVISIONES_VALIDAS))).get_column("revision")))


def construir_gold() -> pl.DataFrame | None:
    """gold/fact_dividendo: lo revisado + lo que tuvo consenso, consolidado por dividendo y cuota."""
    rev = leer_revision()
    if rev is None or rev.is_empty():
        return None
    usables = rev.filter(
        (pl.col("clase_accion") != "")
        & (pl.col("revision").is_in(["aprobado", "corregido"])
           | ((pl.col("revision") == "") & (pl.col("estado_extraccion") == "consenso")))
    )
    filas = []
    for r in usables.iter_rows(named=True):
        fecha_evento = _a_fecha(r["fecha_evento"])
        ejercicio = int(r["ejercicio"]) if r["ejercicio"].isdigit() else None
        filas.append({
            # Los avisos de periodo ex-dividendo no suelen decir el ejercicio. Sin él, cuotas de años
            # distintos se mezclarían; se estima como el año del aviso menos uno (lo que se paga en un
            # año sale de las utilidades del anterior).
            "emisor_id": r["emisor_id"],
            "ejercicio": ejercicio if ejercicio is not None else (fecha_evento.year - 1 if fecha_evento else None),
            "ejercicio_estimado": ejercicio is None,
            "clase_accion": r["clase_accion"], "tipo": r["tipo"], "forma": r["forma"], "moneda": r["moneda"],
            "valor_por_accion": _a_numero(r["valor_por_accion"]), "cuota": int(r["cuota"] or 1),
            "valor_cuota": _a_numero(r["valor_cuota"]), "fecha_ex_dividendo": _a_fecha(r["fecha_ex_dividendo"]),
            "fecha_pago": _a_fecha(r["fecha_pago"]), "fecha_evento": fecha_evento,
            "tipo_documento": r["tipo_documento"], "revisado": r["revision"] in ("aprobado", "corregido"),
            "evento_id": r["evento_id"],
            # prioridad: corrección humana, luego aprobación humana, luego consenso de los agentes
            "prioridad": {"corregido": 0, "aprobado": 1}.get(r["revision"], 2),
        })
    if not filas:
        return None
    df = pl.DataFrame(filas).sort(["prioridad", "fecha_evento"], descending=[False, True])
    clave = ["emisor_id", "ejercicio", "clase_accion", "tipo", "forma", "cuota"]
    gold = df.group_by(clave, maintain_order=True).agg(
        pl.col("moneda").first(),
        # manda lo corregido a mano, luego lo aprobado, luego el documento más reciente;
        # los campos vacíos se completan con los demás documentos
        *[pl.col(c).drop_nulls().first().alias(c) for c in
          ("valor_por_accion", "valor_cuota", "fecha_ex_dividendo", "fecha_pago")],
        (pl.col("tipo_documento") == "proyecto").all().alias("solo_propuesto"),
        pl.col("revisado").any(),
        (
            (pl.col("valor_cuota").drop_nulls().n_unique() > 1)
            | (pl.col("fecha_ex_dividendo").drop_nulls().n_unique() > 1)
            | (pl.col("fecha_pago").drop_nulls().n_unique() > 1)
        ).alias("conflicto_entre_documentos"),
        pl.col("evento_id").unique().str.join(",").alias("eventos"),
        pl.len().alias("documentos"),
        pl.col("ejercicio_estimado").all(),
    )
    # Cuota sin valor propio: el valor por acción se reparte en partes iguales entre las cuotas del
    # decreto (p. ej. "$160 por acción en tres cuotas" = 53,33 cada una), y queda marcado.
    decreto = ["emisor_id", "ejercicio", "clase_accion", "tipo", "forma"]
    gold = gold.with_columns(
        pl.col("valor_cuota").is_null().alias("valor_cuota_estimado"),
        pl.coalesce(pl.col("valor_cuota"), pl.col("valor_por_accion") / pl.col("cuota").max().over(decreto))
        .alias("valor_cuota"),
    )
    storage.escribir_completa("gold", "fact_dividendo", gold.sort(["emisor_id", "ejercicio", "cuota"]))
    return gold
