import csv
from datetime import date, datetime, timezone

import polars as pl
import pytest

from invest import dividendos as dv
from invest import storage
from dividendos_util import CORRECTO, TEXTO_TERPEL, ClienteFalso, pdf_con_texto, variante

MA, MB = "claude-sonnet-5-5", "claude-opus-5-5"
PDF = pdf_con_texto(TEXTO_TERPEL)


def agentes(cliente):
    return dv.Agente("a", "analista", MA, cliente), dv.Agente("b", "auditor", MB, cliente)


# ------------------------------------------------------------------------- comparación
def test_comparar_detecta_lo_que_importa():
    assert dv.comparar(CORRECTO, variante()) == []
    assert dv.comparar(CORRECTO, variante(valor_por_accion=1250.004)) == []  # dentro de la tolerancia
    dif = dv.comparar(CORRECTO, variante(valor_por_accion=1205.0, cuota_fecha_pago="2026-04-16"))
    assert dif == ["ordinaria/ordinario/efectivo valor por acción: A=1250.0 B=1205.0",
                   "ordinaria/ordinario/efectivo cuota 1 fecha_pago: A=2026-04-15 B=2026-04-16"]
    sin_cuota = variante()
    sin_cuota["decretos"][0]["cuotas"].pop()
    assert dv.comparar(CORRECTO, sin_cuota) == ["ordinaria/ordinario/efectivo cuota 2: solo la registra A"]


def test_cita_que_no_esta_en_el_pdf_es_diferencia():
    texto = dv.texto_pdf(PDF)
    assert dv.verificar_citas(CORRECTO, texto, "A") == []
    inventada = variante(cita="Dividendo de $1.300 por accion")
    assert dv.verificar_citas(inventada, texto, "B") == ["ordinaria/ordinario/efectivo: la cita de B no aparece en el PDF"]


def test_alertas_internas():
    malo = variante(cuota_valor=600.0, cuota_fecha_ex_dividendo="2026-04-20")
    assert len(dv.alertas(malo)) == 2


# ------------------------------------------------------------------------- consenso
def test_coinciden_a_la_primera():
    cliente = ClienteFalso({MA: [CORRECTO], MB: [CORRECTO]})
    r = dv.consensuar(PDF, "ctx", *agentes(cliente))
    assert (r.estado, r.rondas, r.final["decretos"][0]["valor_por_accion"]) == ("consenso", 0, 1250.0)
    assert len(cliente.llamadas) == 2


def test_diferencia_se_resuelve_en_revision_cruzada():
    # B leyó mal la fecha de pago; al ver la diferencia corrige. A sostiene su lectura.
    cliente = ClienteFalso({MA: [CORRECTO, CORRECTO], MB: [variante(cuota_fecha_pago="2026-04-16"), CORRECTO]})
    r = dv.consensuar(PDF, "ctx", *agentes(cliente))
    assert (r.estado, r.rondas) == ("consenso", 1)
    pedido_revision = cliente.llamadas[3][1]
    assert "cuota 1 fecha_pago: A=2026-04-15 B=2026-04-16" in pedido_revision
    assert "Registro del otro revisor" in pedido_revision


def test_cita_inventada_obliga_a_revisar():
    cliente = ClienteFalso({MA: [CORRECTO, CORRECTO], MB: [variante(cita="Dividendo de $1.250 por cada accion"), CORRECTO]})
    r = dv.consensuar(PDF, "ctx", *agentes(cliente))
    assert (r.estado, r.rondas) == ("consenso", 1)


def test_sin_acuerdo_queda_en_disputa():
    b = variante(valor_por_accion=1205.0)
    cliente = ClienteFalso({MA: [CORRECTO] * 3, MB: [b] * 3})
    r = dv.consensuar(PDF, "ctx", *agentes(cliente), rondas_maximas=2)
    assert (r.estado, r.rondas, r.final) == ("en_disputa", 2, None)
    assert r.diferencias == ["ordinaria/ordinario/efectivo valor por acción: A=1250.0 B=1205.0"]


def test_documento_sin_dividendos():
    nada = {"contiene_dividendos": False, "tipo_documento": "confirmacion_pago", "ejercicio": 2025,
            "decretos": [], "observaciones": "Solo confirma el pago."}
    r = dv.consensuar(PDF, "ctx", *agentes(ClienteFalso({MA: [nada], MB: [nada]})))
    assert r.estado == "sin_dividendo"


# ------------------------------------------------------------------------- proceso completo y revisión
@pytest.fixture
def evento_terpel(carpeta_datos):
    ahora = datetime(2026, 11, 22, tzinfo=timezone.utc)
    storage.upsert_por_anio("silver", "dim_especie_sfc", pl.DataFrame([{
        "fecha_reporte": date(2026, 11, 18), "especie": "cog20paao006", "nombre": "AO ORGANIZACION TERPEL S.A.",
        "emisor": "cog20", "clase": "ordinaria", "fuente_id": "sfc_especies", "ingest_ts": ahora}]),
        ("fecha_reporte", "especie"), "fecha_reporte")

    def evento(eid, fecha, resumen, archivo):
        return {"evento_id": eid, "fecha": fecha, "hora": "18:01:24", "entidad": "ORGANIZACION TERPEL S.A.",
                "tema": "Decisiones adoptadas por la Asamblea de Inversionistas.", "resumen": resumen,
                "tiene_anexo": True, "anexo_archivo": archivo, "fuente_id": "sfc_informacion_relevante",
                "ingest_ts": ahora}

    filas = [
        evento("e1", date(2026, 3, 18), "La Asamblea aprobó el proyecto de distribución de utilidades.", "sfc/2026/e1.pdf"),
        evento("e2", date(2026, 3, 20), "Se informan las fechas del periodo ex-dividendo.", "sfc/2026/e2.pdf"),
        evento("e3", date(2026, 3, 21), "Cambio de revisor fiscal.", None),  # no es de dividendos
    ]
    storage.upsert_por_anio("silver", "fact_evento_sfc", pl.DataFrame(filas), ("evento_id",), "fecha")
    storage.guardar_anexo("sfc/2026/e1.pdf", PDF)
    storage.guardar_anexo("sfc/2026/e2.pdf", pdf_con_texto(TEXTO_TERPEL[3:]))
    return carpeta_datos


def test_procesar_revisar_y_tabla_de_dividendos(evento_terpel):
    # e1: consenso. e2 (solo fechas): B no ve la segunda cuota ni después de revisar -> disputa
    solo_fechas = variante(tipo_documento="fechas_ex_dividendo", valor_por_accion=None,
                           cita="primera cuota el 15 de abril de 2026 y segunda cuota el 15 de octubre de 2026.")
    b_incompleto = variante(tipo_documento="fechas_ex_dividendo", valor_por_accion=None,
                            cita=solo_fechas["decretos"][0]["cita"])
    b_incompleto["decretos"][0]["cuotas"].pop()
    cliente = ClienteFalso({MA: [CORRECTO, solo_fechas, solo_fechas, solo_fechas],
                            MB: [CORRECTO, b_incompleto, b_incompleto, b_incompleto]})
    conteo = dv.procesar(cliente=cliente)
    assert conteo == {"pendientes": 2, "procesados": 2, "consenso": 1, "sin_dividendo": 0,
                      "en_disputa": 1, "error": 0}

    rev = dv.leer_revision()
    assert rev.get_column("evento_id").to_list() == ["e1", "e1", "e2", "e2"]
    disputa = rev.filter((pl.col("evento_id") == "e2") & (pl.col("cuota") == "2")).row(0, named=True)
    assert disputa["estado_extraccion"] == "en_disputa" and disputa["propuesta_agente_b"] == "valor/acción None COP | sin esta cuota"

    gold = storage.leer_tabla("gold", "fact_dividendo")
    assert gold.height == 2  # solo e1 (consenso); e2 en disputa no entra
    assert gold.get_column("valor_cuota").to_list() == [625.0, 625.0] and not gold.get_column("revisado").any()

    # Una segunda corrida no vuelve a llamar a los agentes
    assert dv.procesar(cliente=ClienteFalso({}))["pendientes"] == 0

    # Revisión humana: aprueba e2 y corrige la fecha de pago de la cuota 1 de e1 (Excel guarda dd/mm/aaaa)
    ruta = dv.ruta_revision()
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        cabecera, *filas = list(csv.reader(f, delimiter=";"))
    i_rev, i_pago, i_ev, i_cuota = (cabecera.index(c) for c in ("revision", "fecha_pago", "evento_id", "cuota"))
    for f in filas:
        if f[i_ev] == "e2":
            f[i_rev] = "aprobado"
        if f[i_ev] == "e1" and f[i_cuota] == "1":
            f[i_pago], f[i_rev] = "16/04/2026", "corregido"
    ruta.write_text("﻿" + "\n".join(";".join(f) for f in [cabecera, *filas]) + "\n", encoding="utf-8")

    dv.exportar()  # la corrida siguiente regenera el archivo sin tocar lo decidido
    rev2 = dv.leer_revision()
    assert set(rev2.filter(pl.col("evento_id") == "e2").get_column("revision")) == {"aprobado"}
    gold = storage.leer_tabla("gold", "fact_dividendo").sort("cuota")
    c1 = gold.row(0, named=True)
    # La corrección manual de e1 manda sobre lo aprobado en e2, y la contradicción queda marcada
    assert c1["fecha_pago"] == date(2026, 4, 16) and c1["conflicto_entre_documentos"] is True
    # e1 aporta el valor por acción que e2 no trae; e2 confirma la fecha ex-dividendo
    assert c1["valor_por_accion"] == 1250.0 and c1["fecha_ex_dividendo"] == date(2026, 4, 9)
    assert c1["documentos"] == 2 and c1["revisado"] is True and c1["solo_propuesto"] is False
    c2 = gold.row(1, named=True)
    assert c2["conflicto_entre_documentos"] is False and c2["fecha_pago"] == date(2026, 10, 15)


def test_sin_clave_de_api_no_falla(evento_terpel, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conteo = dv.procesar()
    assert conteo["omitido"] == "falta ANTHROPIC_API_KEY" and conteo["procesados"] == 0


def test_revision_acepta_formatos_de_excel():
    assert dv._a_numero("1.250,5") == 1250.5 and dv._a_numero("625") == 625.0 and dv._a_numero("") is None
    assert dv._a_fecha("16/04/2026") == dv._a_fecha("2026-04-16") == date(2026, 4, 16)


def test_pedido_a_la_api_lleva_el_pdf_y_fuerza_la_respuesta_estructurada(monkeypatch):
    capturado = {}

    class Bloque:
        type, input = "tool_use", CORRECTO

    class Mensajes:
        def create(self, **kw):
            capturado.update(kw)
            return type("R", (), {"content": [Bloque()]})()

    cliente = dv.ClienteLLM(4000, cliente=type("C", (), {"messages": Mensajes()})())
    assert dv.Agente("b", "auditor", MB, cliente).extraer(PDF, "Entidad: X") == CORRECTO
    contenido = capturado["messages"][0]["content"]
    assert contenido[0]["type"] == "document" and contenido[0]["source"]["media_type"] == "application/pdf"
    assert contenido[1]["type"] == "text" and "Entidad: X" in contenido[1]["text"]
    assert capturado["tool_choice"] == {"type": "tool", "name": "registrar_dividendos"}
    assert capturado["model"] == MB and "auditor" in capturado["system"]


def test_cliente_sin_tool_choice_forzado():
    """Modelos que rechazan tool_choice forzado: se reintenta en automático y se pide la herramienta."""
    from types import SimpleNamespace as NS

    from invest.dividendos import ClienteLLM

    llamadas = []

    class Mensajes:
        def create(self, **kw):
            llamadas.append(kw)
            if kw["tool_choice"]["type"] == "tool":
                raise RuntimeError('Error code: 400 - tool_choice: type "tool" and "any" are not supported for this model.')
            if len(llamadas) == 2:  # primera respuesta automática: solo texto
                return NS(content=[NS(type="text", text="Aquí está el resultado...")])
            return NS(content=[NS(type="tool_use", input={"tipo_documento": "aprobado"})])

    c = ClienteLLM(cliente=NS(messages=Mensajes()))
    assert c.registrar("m", "sistema", b"%PDF", "texto") == {"tipo_documento": "aprobado"}
    assert [k["tool_choice"]["type"] for k in llamadas] == ["tool", "auto", "auto"]
    assert llamadas[1]["system"].endswith("no escribas texto fuera de ella.")
    assert llamadas[2]["messages"][-1]["role"] == "user"
    # La siguiente llamada ya no intenta el modo forzado
    llamadas.clear()
    c.registrar("m", "sistema", b"%PDF", "texto")
    assert [k["tool_choice"]["type"] for k in llamadas] == ["auto"]

    class Otro:
        def create(self, **kw):
            raise RuntimeError("Error code: 401 - invalid x-api-key")

    with pytest.raises(RuntimeError, match="401"):
        ClienteLLM(cliente=NS(messages=Otro())).registrar("m", "s", b"%PDF", "t")


def test_pdf_con_cuerpo_en_imagen_no_bloquea_el_consenso():
    """Solo el encabezado es texto: la cita no se puede encontrar, pero los dos leyeron lo mismo."""
    encabezado = pdf_con_texto(["ORGANIZACION TERPEL S.A.", "Asunto: proyecto de distribucion de utilidades",
                                "Informacion relevante publicada en el Registro Nacional de Valores y Emisores"])
    cliente = ClienteFalso({MA: [CORRECTO], MB: [CORRECTO]})
    r = dv.consensuar(encabezado, "ctx", *agentes(cliente))
    assert (r.estado, r.rondas) == ("consenso", 0)
    assert any("esa parte del PDF es imagen" in a for a in r.alertas)


def test_todas_las_clases_equivale_a_separarlas():
    import copy

    por_clase = copy.deepcopy(CORRECTO)
    pref = copy.deepcopy(por_clase["decretos"][0])
    pref["clase_accion"] = "preferencial"
    por_clase["decretos"].append(pref)
    todas = variante(clase_accion="todas")
    assert dv.comparar(por_clase, todas) == []
    assert dv.comparar(todas, por_clase) == []
    otra = copy.deepcopy(por_clase)
    otra["decretos"][1]["valor_por_accion"] = 999.0
    assert dv.comparar(otra, todas) == ["preferencial/ordinario/efectivo valor por acción: A=999.0 B=1250.0"]


def test_sin_dividendo_ignora_el_tipo_de_documento():
    a = {"contiene_dividendos": False, "tipo_documento": "proyecto", "ejercicio": 2025, "decretos": []}
    b = {**a, "tipo_documento": "aprobado"}
    assert dv.comparar(a, b) == []
    assert dv.comparar(variante(), variante(tipo_documento="proyecto")) == ["tipo_documento: A='aprobado' B='proyecto'"]


def test_gold_reparte_cuotas_y_estima_el_ejercicio(carpeta_datos):
    """Casos reales: Éxito ($160 en tres cuotas sin valor por cuota) y GEB (avisos sin ejercicio)."""
    def fila(ev, fecha, emisor, valor, cuota, valor_cuota="", ex="", pago="", ejercicio="", doc="aprobado"):
        f = {c: "" for c in dv.COLUMNAS_REVISION}
        f.update(evento_id=ev, emisor_id=emisor, fecha_evento=fecha, estado_extraccion="consenso",
                 tipo_documento=doc, ejercicio=ejercicio, clase_accion="todas", tipo="ordinario",
                 forma="efectivo", moneda="COP", valor_por_accion=valor, cuota=str(cuota),
                 valor_cuota=valor_cuota, fecha_ex_dividendo=ex, fecha_pago=pago)
        return f

    filas = [fila("x1", "2026-03-30", "EXITO", "160", n, ex=ex, ejercicio="2025")
             for n, ex in ((1, "2026-07-22"), (2, "2026-09-17"), (3, "2026-12-16"))]
    filas += [fila("g1", "2025-11-25", "GEB", "238", 2, "119", ex="2025-12-09", doc="fechas_ex_dividendo"),
              fila("g2", "2026-07-02", "GEB", "250,8", 1, "150,48", ex="2026-07-23", doc="fechas_ex_dividendo"),
              fila("g2", "2026-07-02", "GEB", "250,8", 2, "100,32", doc="fechas_ex_dividendo")]
    ruta = dv.ruta_revision()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=dv.COLUMNAS_REVISION, delimiter=";")
        w.writeheader()
        w.writerows(filas)
    gold = dv.construir_gold().sort("emisor_id", "ejercicio", "cuota")
    exito = gold.filter(pl.col("emisor_id") == "EXITO")
    assert exito.get_column("valor_cuota").round(2).to_list() == [53.33] * 3
    assert exito.get_column("valor_cuota_estimado").all() and not exito.get_column("ejercicio_estimado").any()
    geb = gold.filter(pl.col("emisor_id") == "GEB")
    assert geb.select("ejercicio", "cuota", "valor_cuota").rows() == [(2024, 2, 119.0), (2025, 1, 150.48), (2025, 2, 100.32)]
    assert geb.get_column("ejercicio_estimado").all()
