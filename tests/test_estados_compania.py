"""Estados publicados por la compañía: descubrimiento, solo lo nuevo, páginas, consenso, unidades,
cuarentena, prioridad del XBRL y cotejo. Sin red ni API: la web y los agentes son falsos."""

from __future__ import annotations

import copy
import hashlib
from datetime import date

import polars as pl
import pytest

from invest import dividendos, fundamentales, storage
from invest.ingest import estados_compania as ec
from invest.ingest import http

HOY = date(2026, 11, 24)
MA, MB = "modelo-a", "modelo-b"


# --------------------------------------------------------------------------- PDF de prueba
def pdf_paginas(paginas: list[list[str]]) -> bytes:
    """PDF con una página por lista de líneas (texto extraíble con pypdf)."""
    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    n = len(paginas)
    objetos: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>",
                            ("<< /Type /Pages /Kids [" + " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
                             + f"] /Count {n} >>").encode(),
                            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    for i, lineas in enumerate(paginas):
        flujo = ("BT /F1 9 Tf 40 760 Td 11 TL " + " ".join(f"({esc(l)}) '" for l in lineas) + " ET").encode("latin-1")
        objetos.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>".encode())
        objetos.append(b"<< /Length " + str(len(flujo)).encode() + b" >>\nstream\n" + flujo + b"\nendstream")
    salida, posiciones = b"%PDF-1.4\n", []
    for i, obj in enumerate(objetos, start=1):
        posiciones.append(len(salida))
        salida += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    salida += b"".join(f"{p:010d} 00000 n \n".encode() for p in posiciones)
    salida += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return salida


def _relleno(n: int) -> list[str]:
    return [f"Partida {i} {1000 + 37 * i:,} {2000 + 41 * i:,}".replace(",", ".") for i in range(n)]


# Grupo Nutresa, 31-dic-2025, millones de pesos (cifras del PDF publicado)
NUTRESA = {"activos": "27.419.325", "pasivos": "22.002.614", "patrimonio_total": "5.416.711",
           "patrimonio_controladora": "5.377.738", "efectivo": "3.195.196", "ingresos_consolidados": "20.578.988",
           "utilidad_total": "1.245.336", "utilidad_controladora": "1.235.765", "upa_basica": "2.705,93"}


def paginas_estados(cifras: dict, unidades: str = "millones de pesos colombianos", consolidado: bool = True,
                    meses: str = "doce meses") -> list[list[str]]:
    tipo = "consolidado" if consolidado else "separado"
    balance_1 = [f"Estado de situacion financiera {tipo}", "Al 31 de diciembre de 2025 y 2024",
                 f"Cifras expresadas en {unidades}", "Activos", f"Efectivo y equivalentes de efectivo {cifras['efectivo']} 2.950.114",
                 *_relleno(12), f"Total activos {cifras['activos']} 26.010.301"]
    balance_2 = ["Pasivos y patrimonio", *_relleno(10), f"Total pasivos {cifras['pasivos']} 21.000.000",
                 f"Patrimonio atribuible a los propietarios de la controladora {cifras['patrimonio_controladora']} 4.950.000",
                 "Participaciones no controladoras 38.973 60.301", f"Total patrimonio {cifras['patrimonio_total']} 5.010.301",
                 "Total pasivo y patrimonio 27.419.325 26.010.301"]
    resultados = [f"Estado de resultados {tipo}", f"Por los {meses} terminados el 31 de diciembre",
                  f"Cifras expresadas en {unidades}", f"Ingresos operacionales {cifras['ingresos_consolidados']} 19.000.000",
                  *_relleno(10), f"Utilidad neta del periodo {cifras['utilidad_total']} 1.100.000",
                  f"Atribuible a participaciones controladoras {cifras['utilidad_controladora']} 1.090.000",
                  f"Utilidad basica por accion (en pesos) {cifras['upa_basica']} 2.380,10"]
    return [balance_1, balance_2, resultados]


def documento(cifras: dict = NUTRESA, **kw) -> bytes:
    portada = ["Grupo Nutresa S. A.", "Estados financieros consolidados", "a 31 de diciembre de 2025 y 2024"]
    indice = ["Contenido", "Estado de situacion financiera consolidado ........ 9",
              "Estado de resultados consolidado ........ 10", "Notas ........ 12"]
    revisor = ["Informe del revisor fiscal", "Hemos auditado los estados financieros consolidados", "19 de febrero de 2026"]
    notas = [["Nota 5 Efectivo", "El estado de situacion financiera incluye total activos por", *_relleno(14)]]
    return pdf_paginas([portada, indice, revisor, *paginas_estados(cifras, **kw), *notas])


# --------------------------------------------------------------------------- registros de los agentes
def registro(cifras: dict = NUTRESA, unidades: str = "millones", corte: str = "2025-12-31", meses: int = 12,
             moneda: str = "COP", **cambios) -> dict:
    def numero(t: str) -> float:
        return float(t.replace(".", "").replace(",", "."))

    r = {"es_consolidado": True, "fecha_corte": corte, "moneda": moneda, "unidades": unidades,
         "meses_acumulados": meses, "fecha_informe_revisor": "2026-02-19", "observaciones": "",
         "cifras": {c: {"valor": numero(v), "pagina": 1 if c in ec.BALANCE else 3, "rotulo": c} for c, v in cifras.items()}}
    for k, v in cambios.items():
        if k in ec.CONCEPTOS:
            r["cifras"][k]["valor"] = v
        else:
            r[k] = v
    return r


class ClienteFalso:
    """Respuestas guionizadas por modelo; registra lo que se pidió y cuántas páginas se enviaron."""

    def __init__(self, guion: dict[str, list[dict]]):
        self.guion = {m: list(r) for m, r in guion.items()}
        self.llamadas: list[tuple[str, str, int]] = []

    def registrar(self, modelo, sistema, pdf, texto):
        assert pdf.startswith(b"%PDF")
        self.llamadas.append((modelo, texto, len(ec.textos_paginas(pdf))))
        return copy.deepcopy(self.guion[modelo].pop(0))


# --------------------------------------------------------------------------- web y configuración falsas
class Web:
    """obtener_condicional falso: url -> bytes | (bytes, last_modified) | excepción. Sin entrada: 404."""

    def __init__(self, paginas: dict):
        self.paginas = dict(paginas)
        self.pedidos: list[str] = []

    def __call__(self, url, previo=None, reintentos=4, timeout=60, cabeceras=None):
        self.pedidos.append(url)
        r = self.paginas.get(url)
        if r is None:
            raise http.NoEncontrado(f"404 en {url}")
        if isinstance(r, Exception):
            raise r
        contenido, lm = r if isinstance(r, tuple) else (r, None)
        validadores = {"etag": hashlib.md5(contenido).hexdigest(), **({"last_modified": lm} if lm else {})}
        if previo and previo.get("etag") == validadores["etag"]:
            return None, dict(previo)
        return contenido, validadores


INDICE_NUTRESA = "https://nutresa.test/en/investors/quarterly-results"
PDF_NUTRESA = "https://gruponutresa.com/wp-content/uploads/2026/02/Grupo-Nutresa-EEFF-Consolidados-2025.pdf"
HTML_NUTRESA = f"""<html><body>
<a href="{PDF_NUTRESA}">4Q25 Consolidated Financial Results</a>
<a href="/wp-content/uploads/2026/02/Grupo-Nutresa-EEFF-Separados-2025.pdf">4Q25 Separate Financial Statements</a>
<a href="https://gruponutresa.com/wp-content/uploads/2026/02/ESP-Grupo-Nutresa-Presentacion-de-inversionistas-4Q25.pdf">Presentation</a>
<script>var docs = {{"url":"https:\\/\\/gruponutresa.com\\/wp-content\\/uploads\\/2025\\/07\\/Grupo-Nutresa-Estados-Financieros-Consolidados-Junio.pdf"}};</script>
</body></html>""".encode()


@pytest.fixture
def cfg(monkeypatch):
    real = ec.config()
    emisores = ec.emisores_config(real)
    nutresa = {**emisores["CO-COT04"], "paginas_indice": [{"url": INDICE_NUTRESA}]}
    mineros = {"emisor_id": "CO-COC07", "nombre": "Mineros", "descarga": "automatica", "moneda": "USD",
               "unidades_esperadas": ["miles"], "paginas_indice": [],
               "plantillas": ["https://mineros.test/fs-{t}q{anio}.pdf"]}
    ecopetrol = {**emisores["CO-COC04"]}
    cementos = {**emisores["CO-COD38"]}
    prueba = {**real, "agentes": {"a": {"nombre": "analista", "modelo": MA}, "b": {"nombre": "auditor", "modelo": MB}},
              "emisores": [nutresa, mineros, ecopetrol, cementos]}
    monkeypatch.setattr(ec, "config", lambda: copy.deepcopy(prueba))
    return prueba


def correr(web: Web, cliente, monkeypatch, hoy: date = HOY, **kw):
    monkeypatch.setattr(http, "obtener_condicional", web)
    return ec.ConectorEstadosCompania(cliente=cliente, hoy=hoy).ejecutar(**kw)


def cuarentena() -> pl.DataFrame:
    carpeta = storage.data_dir() / "cuarentena" / ec.TABLA
    archivos = sorted(carpeta.glob("*.parquet")) if carpeta.exists() else []
    return pl.concat([pl.read_parquet(a) for a in archivos]) if archivos else pl.DataFrame()


# --------------------------------------------------------------------------- descubrimiento
def test_descubre_los_pdf_consolidados_desde_el_html(cfg):
    lista = ec.enlaces(HTML_NUTRESA.decode(), INDICE_NUTRESA)
    urls = [u for u, _ in lista]
    assert "https://nutresa.test/wp-content/uploads/2026/02/Grupo-Nutresa-EEFF-Separados-2025.pdf" in urls  # relativo
    assert any("Consolidados-Junio.pdf" in u and "\\/" not in u for u in urls)  # dentro del JSON de la página
    nutresa = ec.emisores_config(cfg)["CO-COT04"]
    cands = ec.candidatos(nutresa, lista)
    assert sorted(c["url"].rsplit("/", 1)[1] for c in cands) == [
        "Grupo-Nutresa-EEFF-Consolidados-2025.pdf", "Grupo-Nutresa-Estados-Financieros-Consolidados-Junio.pdf"]
    cortes = {c["url"].rsplit("/", 1)[1]: c["corte"] for c in cands}
    assert cortes["Grupo-Nutresa-EEFF-Consolidados-2025.pdf"] == date(2025, 12, 31)
    assert cortes["Grupo-Nutresa-Estados-Financieros-Consolidados-Junio.pdf"] == date(2025, 6, 30)  # carpeta 2025/07


@pytest.mark.parametrize("url,corte", [
    ("https://files.grupoargos.com/uploads-grupo-argos/2026/05/Estados_Financieros_Consolidados_Grupo_Argos_Marzo_2026.pdf", date(2026, 3, 31)),
    ("https://files.grupoargos.com/uploads-grupo-argos/2026/03/Grupo-Argos-Estados-Financieros-Consolidados-al-31-de-diciembre-2025.pdf", date(2025, 12, 31)),
    ("https://www.grupoexito.com.co/es/estados/financieros/consolidados/3t2026.pdf", date(2026, 9, 30)),
    ("https://files.ecopetrol.com.co/web/esp/estadosfinancieros/balance-gee2q2026.pdf", date(2026, 6, 30)),
    ("https://argos.co/wp-content/uploads/2026/02/Reporte-4T25-pw2.pdf", date(2025, 12, 31)),
    ("https://gruponutresa.com/wp-content/uploads/2025/11/Circular-012-Q3-2025.pdf", date(2025, 9, 30)),
    ("https://x.test/uploads/2026/08/informe.pdf", date(2026, 6, 30)),  # solo la carpeta de carga
])
def test_corte_probable(url, corte):
    assert ec.corte_probable(url) == corte


def test_plantillas_solo_cortes_ya_vencidos(cfg):
    exito = {"plantillas": ["https://www.grupoexito.com.co/es/estados/financieros/consolidados/{t}t{anio}.pdf"]}
    urls = [c["url"].rsplit("/", 1)[1] for c in ec.urls_plantilla(exito, date(2026, 10, 20), 3, 25)]
    assert urls == ["2t2026.pdf", "1t2026.pdf", "4t2025.pdf"]  # el 3t2026 aún no se espera el 20-oct


def test_fecha_de_publicacion_detectada():
    assert ec.fecha_publicacion(PDF_NUTRESA, "Thu, 19 Feb 2026 22:10:00 GMT") == (date(2026, 2, 19), "last_modified")
    assert ec.fecha_publicacion(PDF_NUTRESA, None) == (date(2026, 2, 28), "carpeta")  # fin del mes de carga
    assert ec.fecha_publicacion("https://x.test/3t2026.pdf", None) == (None, None)


# --------------------------------------------------------------------------- páginas
def test_localiza_las_paginas_de_los_estados_consolidados(cfg):
    separados = paginas_estados(NUTRESA, consolidado=False)
    pdf = pdf_paginas([["Portada"], ["Contenido", "Estado de situacion financiera ..... 4"], *separados,
                       *paginas_estados(NUTRESA), ["Nota 1", *_relleno(20)]])
    textos = ec.textos_paginas(pdf)
    assert ec.localizar_paginas(textos, cfg) == [5, 6, 7]  # balance (2 páginas) y resultados consolidados
    assert len(ec.textos_paginas(ec.recortar(pdf, [5, 6, 7]))) == 3
    con_limite = {**cfg, "limites": {**cfg["limites"], "max_paginas_por_documento": 2}}
    assert ec.localizar_paginas(textos, con_limite) == [5, 6]


# --------------------------------------------------------------------------- de punta a punta
def test_consenso_unidades_y_solo_lo_nuevo(cfg, carpeta_datos, monkeypatch):
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: (documento(), "Thu, 19 Feb 2026 22:10:00 GMT")})
    cliente = ClienteFalso({MA: [registro()], MB: [registro()]})
    res = correr(web, cliente, monkeypatch)
    assert res.estado == "ok", res.detalle
    # Solo se enviaron las tres páginas de los estados, no las siete del PDF
    assert [n for _, _, n in cliente.llamadas] == [3, 3]
    assert "Corte probable según el nombre del archivo: 2025-12-31" in cliente.llamadas[0][1]
    silver = storage.leer_tabla("silver", ec.TABLA)
    assert silver.height == 9 and set(silver.get_column("estado")) == {"consenso"}
    fila = silver.filter(pl.col("concepto") == "activos").row(0, named=True)
    assert fila["valor"] == 27_419_325e6 and fila["unidad"] == "pesos" and fila["unidades_documento"] == "millones"
    assert fila["pagina"] == 4 and fila["fecha_publicacion"] == date(2026, 2, 19)  # página del PDF original
    assert fila["archivo"].startswith("anexos/companias/CO-COT04/2025-12-31_")
    assert (carpeta_datos / fila["archivo"]).exists()
    upa = silver.filter(pl.col("concepto") == "upa_basica").row(0, named=True)
    assert upa["valor"] == 2705.93 and upa["unidad"] == "pesos_por_accion"
    assert "consenso 1" in res.detalle and "27419325" not in res.detalle  # las notas solo llevan conteos
    # El junio sin año: en la página pero no en el servidor (404): se anota y no se vuelve a pedir en 7 días
    pedidos = len(web.pedidos)
    res2 = correr(web, ClienteFalso({MA: [], MB: []}), monkeypatch)
    assert res2.estado == "sin_novedades"
    nuevos = web.pedidos[pedidos:]
    assert PDF_NUTRESA not in nuevos and INDICE_NUTRESA in nuevos  # el índice se pide condicional (304)
    assert not any("Consolidados-Junio" in u for u in nuevos)
    # Pasados 7 días se reintenta el 404, pero el PDF ya guardado no
    correr(web, ClienteFalso({MA: [], MB: []}), monkeypatch, hoy=date(2026, 12, 2))
    assert any("Consolidados-Junio" in u for u in web.pedidos) and web.pedidos.count(PDF_NUTRESA) == 1


def test_mineros_en_miles_de_dolares(cfg, carpeta_datos, monkeypatch):
    cifras = {"activos": "1.250.400", "pasivos": "450.100", "patrimonio_total": "800.300",
              "patrimonio_controladora": "790.200", "efectivo": "210.500", "ingresos_consolidados": "559.300",
              "utilidad_total": "133.200", "utilidad_controladora": "131.900", "upa_basica": "0,44"}
    pdf = pdf_paginas(paginas_estados(cifras, unidades="thousands of US dollars", meses="nine months"))
    web = Web({"https://mineros.test/fs-3q2026.pdf": pdf})
    r = registro(cifras, unidades="miles", corte="2026-09-30", meses=9, moneda="USD")
    res = correr(web, ClienteFalso({MA: [r], MB: [r]}), monkeypatch)
    assert res.estado == "ok", res.detalle
    silver = storage.leer_tabla("silver", ec.TABLA).filter(pl.col("emisor_id") == "CO-COC07")
    activos = silver.filter(pl.col("concepto") == "activos").row(0, named=True)
    assert activos["valor"] == 1_250_400e3 and activos["unidad"] == "usd"
    assert silver.filter(pl.col("concepto") == "upa_basica").row(0, named=True)["unidad"] == "usd_por_accion"
    assert activos["fecha_publicacion"] is None  # sin Last-Modified ni carpeta: lo decide fundamentales


def test_revision_cruzada_llega_al_consenso(cfg, carpeta_datos, monkeypatch):
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: documento()})
    # A toma la columna del comparativo en los activos; al ver la diferencia, corrige
    cliente = ClienteFalso({MA: [registro(activos=26010301.0), registro()], MB: [registro(), registro()]})
    res = correr(web, cliente, monkeypatch)
    assert res.estado == "ok" and len(cliente.llamadas) == 4
    assert "activos: A=26010301.0 B=27419325.0" in cliente.llamadas[2][1]
    log = storage.leer_tabla("silver", ec.TABLA_EXTRACCION).row(0, named=True)
    assert log["estado"] == "consenso" and log["rondas"] == 1 and log["paginas"] == "3,4,5"


def test_sin_consenso_va_a_cuarentena(cfg, carpeta_datos, monkeypatch):
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: documento()})
    otra = registro(unidades="miles")
    cliente = ClienteFalso({MA: [registro()] * 3, MB: [otra] * 3})
    res = correr(web, cliente, monkeypatch)
    assert res.estado == "revisar" and len(cliente.llamadas) == 6  # 2 lecturas + 2 rondas
    assert storage.leer_tabla("silver", ec.TABLA) is None
    q = cuarentena()
    assert q.height == 9 and set(q.get_column("motivo")) == {"sin_consenso"} and set(q.get_column("estado")) == {"en_disputa"}
    # La disputa no se vuelve a enviar (cuesta): queda para revisión
    otro = ClienteFalso({MA: [], MB: []})
    correr(web, otro, monkeypatch)
    assert otro.llamadas == []


def test_cifra_que_no_esta_en_el_texto_no_tiene_consenso(cfg, carpeta_datos, monkeypatch):
    """Los dos coinciden en una cifra calculada que el documento no trae: se revisa y queda en disputa."""
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: documento()})
    inventada = registro(efectivo=3195197.0)
    cliente = ClienteFalso({MA: [inventada] * 3, MB: [inventada] * 3})
    correr(web, cliente, monkeypatch)
    log = storage.leer_tabla("silver", ec.TABLA_EXTRACCION).row(0, named=True)
    assert log["estado"] == "en_disputa" and "efectivo: la cifra 3195197.0 no aparece" in log["diferencias"]


def test_controles_mandan_a_cuarentena(cfg, carpeta_datos, monkeypatch):
    cifras = {**NUTRESA, "pasivos": "20.002.614", "patrimonio_controladora": "5.500.000"}  # no cuadra; controladora > total
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: documento(cifras)})
    r = registro(cifras, meses=9)  # resultados de 9 meses en un cierre anual
    res = correr(web, ClienteFalso({MA: [r], MB: [r]}), monkeypatch)
    assert res.estado == "revisar"
    motivos = dict(cuarentena().select("concepto", "motivo").iter_rows())
    assert motivos["activos"] == motivos["pasivos"] == motivos["patrimonio_total"] == "control_balance"
    assert motivos["patrimonio_controladora"] == "control_controladora"
    assert motivos["utilidad_controladora"] == "resultados_no_acumulados"
    assert storage.leer_tabla("silver", ec.TABLA).get_column("concepto").to_list() == ["efectivo"]


def test_controles_de_signo():
    emisor = {"moneda": "COP", "unidades_esperadas": ["millones"]}
    r = registro(efectivo=-5.0, upa_basica=-10.0)
    motivos, _ = ec.controles(r, emisor, {})
    assert motivos == {"efectivo": "control_signo", "upa_basica": "control_signo"}
    motivos, avisos = ec.controles(registro(unidades="miles"), emisor, {})
    assert motivos == {} and "miles" in avisos[0]  # otra unidad: solo aviso
    assert set(ec.controles(registro(moneda="USD"), emisor, {})[0].values()) == {"moneda_inesperada"}


def test_sin_api_key_baja_pero_no_lee(cfg, carpeta_datos, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: documento()})
    res = correr(web, None, monkeypatch)
    assert "falta ANTHROPIC_API_KEY" in res.detalle and storage.leer_tabla("silver", ec.TABLA) is None
    assert (carpeta_datos / "anexos" / "companias" / "CO-COT04").exists()
    # Con la clave (aquí, el cliente falso) se lee sin volver a bajar
    cliente = ClienteFalso({MA: [registro()], MB: [registro()]})
    correr(web, cliente, monkeypatch)
    assert web.pedidos.count(PDF_NUTRESA) == 1 and storage.leer_tabla("silver", ec.TABLA).height == 9


def test_limite_de_documentos_por_corrida(cfg, carpeta_datos, monkeypatch):
    cfg["limites"]["max_documentos_por_corrida"] = 1
    junio = "https://gruponutresa.com/wp-content/uploads/2025/07/Grupo-Nutresa-Estados-Financieros-Consolidados-Junio.pdf"
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: documento(), junio: documento()})
    cliente = ClienteFalso({MA: [registro()], MB: [registro()]})
    res = correr(web, cliente, monkeypatch)
    assert len(cliente.llamadas) == 2 and "1 documentos quedan para la próxima corrida" in res.detalle
    # Primero el corte más reciente
    assert storage.leer_tabla("silver", ec.TABLA).get_column("fecha_corte").unique().to_list() == [date(2025, 12, 31)]


def test_bloqueo_contra_robots_no_se_esquiva(cfg, carpeta_datos, monkeypatch):
    desafio = b"<html><head><META NAME='ROBOTS' CONTENT='NOINDEX'><script src='/_Incapsula_Resource?x=1'></script></head></html>"
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: desafio})
    res = correr(web, ClienteFalso({MA: [], MB: []}), monkeypatch)
    assert "protección contra robots" in res.detalle
    estado = storage.leer_estado_fuente(ec.FUENTE_ID)
    assert "CO-COT04" in estado["bloqueos"] and PDF_NUTRESA not in estado.get("documentos", {})
    assert not any("Consolidados-Junio" in u for u in web.pedidos)  # se detiene ese emisor en la corrida
    # En las corridas de los días siguientes ni siquiera se pide la página índice (pausa de 7 días)
    pedidos = len(web.pedidos)
    res = correr(web, ClienteFalso({MA: [], MB: []}), monkeypatch, hoy=date(2026, 11, 26))
    assert not any("nutresa" in u for u in web.pedidos[pedidos:]) and "emisores_en_pausa_por_bloqueo 1" in res.detalle


def test_pdf_puesto_a_mano(cfg, carpeta_datos, monkeypatch):
    carpeta = carpeta_datos / "fuentes_compania" / "CO-COD38"
    carpeta.mkdir(parents=True)
    (carpeta / "Reporte-4T25.pdf").write_bytes(documento())
    web = Web({})
    res = correr(web, ClienteFalso({MA: [registro()], MB: [registro()]}), monkeypatch)
    assert res.estado == "ok", res.detalle
    assert not any("argos.co" in u for u in web.pedidos)  # descarga manual: no se pide nada al sitio
    fila = storage.leer_tabla("silver", ec.TABLA).filter(pl.col("emisor_id") == "CO-COD38").row(0, named=True)
    assert fila["archivo"] == "fuentes_compania/CO-COD38/Reporte-4T25.pdf" and fila["origen_fecha_publicacion"] == "manual"


def test_balance_y_resultados_en_pdf_separados(cfg, carpeta_datos, monkeypatch):
    """Ecopetrol: un PDF con el balance y otro con resultados; se espera a tener los dos y van juntos."""
    cifras = {"activos": "284.099", "pasivos": "174.499", "patrimonio_total": "109.600",
              "patrimonio_controladora": "84.938", "efectivo": "8.745", "ingresos_consolidados": "68.823",
              "utilidad_total": "10.912", "utilidad_controladora": "8.951", "upa_basica": "217,7"}
    balance, _, resultados = paginas_estados(cifras, unidades="miles de millones de pesos colombianos")
    _, balance_2, _ = paginas_estados(cifras)
    base = "https://files.ecopetrol.com.co/web/esp/estadosfinancieros/"
    web = Web({base + "balance-gee2q2026.pdf": pdf_paginas([balance, balance_2])})
    cliente = ClienteFalso({MA: [], MB: []})
    correr(web, cliente, monkeypatch)
    assert cliente.llamadas == []  # falta el de resultados
    web.paginas[base + "pyg-gee-2q2026.pdf"] = pdf_paginas([resultados])
    r = registro(cifras, unidades="miles_de_millones", corte="2026-06-30", meses=6)
    cliente = ClienteFalso({MA: [r], MB: [r]})
    res = correr(web, cliente, monkeypatch, hoy=date(2026, 12, 2))  # el 404 de la plantilla se reintenta a los 7 días
    assert res.estado == "ok" and [n for _, _, n in cliente.llamadas] == [3, 3]
    fila = storage.leer_tabla("silver", ec.TABLA).filter(pl.col("concepto") == "activos").row(0, named=True)
    assert fila["valor"] == 284_099e9 and ";" in fila["archivo"]


def test_cliente_pide_la_herramienta_de_estados():
    capturado = {}

    class Mensajes:
        def create(self, **kw):
            capturado.update(kw)
            return type("R", (), {"content": [type("B", (), {"type": "tool_use", "input": {"x": 1}})()]})()

    cliente = dividendos.ClienteLLM(100, cliente=type("C", (), {"messages": Mensajes()})(), herramienta=ec.HERRAMIENTA)
    agente = ec.AgenteEE("b", "auditor", MB, cliente)
    assert agente.extraer(b"%PDF-1.4", "Emisor: X") == {"x": 1}
    assert capturado["tool_choice"] == {"type": "tool", "name": "registrar_estados_financieros"}
    assert "auditor" in capturado["system"] and capturado["tools"][0]["input_schema"] is ec.ESQUEMA


# --------------------------------------------------------------------------- fundamentales
def _silver(emisor, corte, concepto, valor, unidad="pesos", publicada=None, descubierto=None):
    return {"emisor_id": emisor, "fecha_corte": corte, "concepto": concepto, "valor": valor, "unidad": unidad,
            "valor_documento": None, "unidades_documento": "millones", "archivo": "a.pdf", "pagina": 1,
            "fecha_publicacion": publicada, "origen_fecha_publicacion": None, "fecha_descubierto": descubierto,
            "estado": "consenso", "documento_id": "d", "huella_pdf": "h", "fuente_id": ec.FUENTE_ID, "ingest_ts": None}


def _xbrl(emisor, corte, concepto, valor, escala="miles_de_pesos"):
    return {"emisor_id": emisor, "nit": "", "entidad_sfc": "", "fecha_corte": corte, "tipo_informe": "consolidado",
            "concepto": concepto, "tipo_periodo": "instante", "fecha_inicio": corte, "fecha_fin": corte, "valor": valor,
            "unidad": "COP", "escala": escala, "radicado": "1", "fecha_transmision": date(2026, 8, 12),
            "fuente_id": "sfc_rnve_niif", "ingest_ts": None}


def test_fecha_disponible_del_pdf():
    ee = pl.DataFrame([
        _silver("CO-COT04", date(2025, 12, 31), "activos", 1.0, publicada=date(2026, 2, 19)),
        _silver("CO-COT04", date(2025, 9, 30), "activos", 1.0),                           # corte + 60
        _silver("CO-COT04", date(2024, 12, 31), "activos", 1.0),                          # cierre: + 90
        _silver("CO-COT04", date(2025, 6, 30), "activos", 1.0, descubierto=date(2025, 8, 1)),  # se tuvo antes
    ], schema=ec.ESQUEMA_SILVER | {"ingest_ts": pl.Datetime("us", "UTC")})
    df = fundamentales.desde_pdf_compania(ee).sort("fecha_corte")
    assert df.get_column("fecha_disponible").to_list() == [date(2025, 3, 31), date(2025, 8, 1), date(2025, 11, 29),
                                                           date(2026, 2, 19)]
    assert set(df.get_column("fuente")) == {"pdf_compania"} and set(df.get_column("base")) == {"consolidado"}


def test_manda_el_xbrl_y_cotejo(carpeta_datos, cfg):
    junio, marzo = date(2026, 6, 30), date(2026, 3, 31)
    ee = pl.DataFrame([
        # Grupo Argos (sin XBRL): entra
        _silver("CO-COT09", marzo, "activos", 37_291_233_963e3),
        # ISA con XBRL en junio: el PDF no entra ese corte, pero sí se coteja
        _silver("CO-COE15", junio, "activos", 80_000_000e6),
        _silver("CO-COE15", junio, "pasivos", 49_000_000e6),
        _silver("CO-COE15", marzo, "activos", 79_000_000e6),
        # Ecopetrol, control: no entra nunca a fact_fundamental
        _silver("CO-COC04", marzo, "activos", 280_000e9),
        _silver("CO-COC04", junio, "activos", 284_099e9),
        _silver("CO-COC04", junio, "upa_basica", 217.7, unidad="pesos_por_accion"),
    ], schema=ec.ESQUEMA_SILVER | {"ingest_ts": pl.Datetime("us", "UTC")})
    xbrl = pl.DataFrame([
        _xbrl("CO-COE15", junio, "ifrs:Assets", 80_010_000_000.0),       # 80,01 billones: +0,0125 % ok
        _xbrl("CO-COE15", junio, "ifrs:Liabilities", 50_000_000_000.0),  # 2 % de diferencia: revisar
        _xbrl("CO-COC04", junio, "ifrs:Assets", 284_099_400_000.0),
        {**_xbrl("CO-COC04", junio, "ifrs:BasicEarningsLossPerShare", 217.73, escala="pesos"), "tipo_periodo": "duracion",
         "fecha_inicio": date(2026, 1, 1)},
    ])
    storage.upsert_por_anio("silver", "fact_ee_compania", ee, ("emisor_id", "fecha_corte", "concepto"), "fecha_corte")
    storage.upsert_por_anio("silver", "fact_xbrl_sfc", xbrl, ("emisor_id", "fecha_corte", "concepto"), "fecha_corte")
    df, resumen = fundamentales.construir()
    pdf = df.filter(pl.col("fuente") == "pdf_compania")
    assert sorted(pdf.select("emisor_id", "fecha_corte").unique().rows()) == [("CO-COE15", marzo), ("CO-COT09", marzo)]
    # Una sola cifra por emisor × corte × base × concepto
    assert df.group_by(["emisor_id", "fecha_corte", "base", "concepto"]).len().get_column("len").max() == 1
    c = storage.leer_tabla("gold", "fact_cotejo_pdf_compania")
    estados = {(e, k): s for e, k, s in c.select("emisor_id", "concepto", "estado").iter_rows()}
    assert estados == {("CO-COE15", "activos"): "ok", ("CO-COE15", "pasivos"): "revisar",
                       ("CO-COC04", "activos"): "ok", ("CO-COC04", "upa_basica"): "ok"}
    assert resumen["con_pdf_compania"] == 2 and resumen["cotejo_pdf_ok"] == 3 and resumen["cotejo_pdf_revisar"] == 1


def test_cotejo_vacio_se_escribe(carpeta_datos):
    storage.upsert_por_anio("silver", "fact_xbrl_sfc", pl.DataFrame([_xbrl("CO-COE15", date(2026, 6, 30), "ifrs:Assets", 1.0)]),
                            ("emisor_id", "fecha_corte", "concepto"), "fecha_corte")
    fundamentales.construir()
    c = storage.leer_tabla("gold", "fact_cotejo_pdf_compania")
    assert c is not None and c.is_empty() and "estado" in c.columns


def test_pdf_resubido_vale_el_plazo():
    # Mineros resubió en sep-2024 los estados de 2022: la fecha del archivo no es la de publicación
    ee = pl.DataFrame([
        _silver("CO-COC07", date(2022, 12, 31), "activos", 1.0, publicada=date(2024, 9, 6)),
        _silver("CO-COC07", date(2024, 9, 30), "activos", 1.0, publicada=date(2025, 2, 7)),  # tarde, pero no resubido
    ], schema=ec.ESQUEMA_SILVER | {"ingest_ts": pl.Datetime("us", "UTC")})
    df = fundamentales.desde_pdf_compania(ee).sort("fecha_corte")
    assert df.get_column("fecha_disponible").to_list() == [date(2023, 3, 31), date(2025, 2, 7)]


def test_incluye_el_estado_de_flujos_si_hay_espacio(cfg):
    flujos = ["Estado de flujos de efectivo consolidado", "Por los doce meses terminados el 31 de diciembre",
              "Utilidad neta del periodo 1.100.000", "Depreciacion y amortizacion 650.300 600.100", *_relleno(14)]
    pdf = pdf_paginas([["Portada"], *paginas_estados(NUTRESA), flujos, ["Nota 1", *_relleno(20)]])
    textos = ec.textos_paginas(pdf)
    assert ec.localizar_paginas(textos, cfg) == [1, 2, 3, 4]
    con_limite = {**cfg, "limites": {**cfg["limites"], "max_paginas_por_documento": 3}}
    assert ec.localizar_paginas(textos, con_limite) == [1, 2, 3]  # sin espacio, el de flujos queda fuera


def test_documento_leido_con_version_anterior_se_vuelve_a_leer(cfg, carpeta_datos, monkeypatch):
    web = Web({INDICE_NUTRESA: HTML_NUTRESA, PDF_NUTRESA: (documento(), "Thu, 19 Feb 2026 22:10:00 GMT")})
    correr(web, ClienteFalso({MA: [registro()], MB: [registro()]}), monkeypatch)
    log = storage.leer_tabla("silver", ec.TABLA_EXTRACCION)
    assert ec.version_de(log.row(0, named=True)) == ec.VERSION_LECTURA
    # Como lo dejó la versión 1 (sin la marca): se lee otra vez, una sola vez
    viejo = log.with_columns(pl.col("json_a").str.replace(r', "version_lectura": \d+', ""),
                             pl.col("json_b").str.replace(r', "version_lectura": \d+', ""),
                             pl.col("json_final").str.replace(r', "version_lectura": \d+', ""))
    assert ec.version_de(viejo.row(0, named=True)) == 1
    storage.escribir_por_anio("silver", ec.TABLA_EXTRACCION, viejo, "fecha_referencia")
    cliente = ClienteFalso({MA: [registro()], MB: [registro()]})
    correr(web, cliente, monkeypatch)
    assert len(cliente.llamadas) == 2
    otra = ClienteFalso({MA: [], MB: []})
    correr(web, otra, monkeypatch)
    assert otra.llamadas == []


def test_separados_se_aceptan_solo_si_el_emisor_no_tiene_consolidados():
    emisor = {"emisor_id": "CO-COD12", "moneda": "COP", "unidades_esperadas": ["millones"]}
    r = registro(es_consolidado=False)
    assert "no_consolidado" in set(ec.controles(r, emisor, {})[0].values())
    assert "no_consolidado" not in set(ec.controles(r, {**emisor, "acepta_separados": True}, {})[0].values())
