from datetime import date

import pytest

from invest.ingest import bvc, estados_ventana as ev

PDF = b"%PDF-1.4\n% estados\n"
DESAFIO = b"<html><script src='/_Incapsula_Resource?x=1'></script></html>"
INDICE = """<html><body>
<a href="/wp-content/uploads/2026/05/Reporte-1T26.pdf">Reporte 1T26</a>
<a href="/wp-content/uploads/2026/05/Estados-Financieros-Separados-1T26.pdf">Separados</a>
<a href="/wp-content/uploads/2026/02/Estados-Financieros-Consolidados-2025.pdf">Consolidados 2025</a>
</body></html>"""


class Respuesta:
    def __init__(self, cuerpo, url="", ok=True, tipo="application/pdf"):
        self._c, self.url, self.ok, self.headers = cuerpo, url, ok, {"content-type": tipo}

    def body(self):
        return self._c


class Pagina:
    def __init__(self, nav):
        self.nav, self.url, self.vistas = nav, "", 0

    def goto(self, url, **kw):
        self.url = url
        self.nav.visitas.append(url)

    def content(self):
        self.vistas += 1
        # La primera vez el sitio muestra la verificación; la persona la resuelve y aparece la página
        if self.nav.desafio and self.vistas == 1:
            return DESAFIO.decode()
        return INDICE

    def wait_for_timeout(self, ms):
        pass

    def wait_for_event(self, evento, timeout=None):
        raise TimeoutError

    def close(self):
        pass


class Peticiones:
    def __init__(self, nav):
        self.nav = nav

    def get(self, url, timeout=None):
        self.nav.descargas.append(url)
        return Respuesta(PDF, url)


class Contexto:
    def __init__(self, nav):
        self.nav, self.request = nav, Peticiones(nav)

    def on(self, evento, f):
        self.nav.oyente = f

    def remove_listener(self, evento, f):
        pass

    def new_page(self):
        p = Pagina(self.nav)
        if self.nav.abre_pdf:  # la persona abre un informe en la ventana
            self.nav.oyente(Respuesta(PDF, "https://argos.co/wp-content/uploads/2025/11/Reporte-3T25-ES.pdf"))
        return p


class Navegador:
    def __init__(self, desafio=False, abre_pdf=False):
        self.desafio, self.abre_pdf = desafio, abre_pdf
        self.visitas, self.descargas, self.sin_ventana, self.oyente = [], [], None, None

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    @property
    def chromium(self):
        return self

    def launch(self, headless=True):
        self.sin_ventana = headless
        return self

    def new_context(self, **kw):
        return Contexto(self)

    def close(self):
        pass


@pytest.fixture
def persona(monkeypatch):
    monkeypatch.setenv("INVEST_LECTURA_MANUAL", "1")


def test_solo_la_lanza_una_persona(carpeta_datos, monkeypatch):
    monkeypatch.delenv("INVEST_LECTURA_MANUAL", raising=False)
    monkeypatch.setattr(bvc, "hay_consola", lambda: False)
    with pytest.raises(ev.SinPersona):
        ev.leer(["CO-COD38"], navegador_factory=Navegador(), avisar=lambda m: None)


def test_ventana_visible_espera_la_verificacion_y_guarda_consolidados(carpeta_datos, persona):
    nav = Navegador(desafio=True, abre_pdf=True)
    mensajes = []
    r = ev.leer(["CO-COD38"], abierta_s=1, navegador_factory=nav, avisar=mensajes.append)
    assert nav.sin_ventana is False  # siempre con ventana
    assert any("resuélvala en la ventana" in m for m in mensajes)  # no se toca la verificación: se espera
    # Guarda el reporte y los consolidados, no los separados; más el PDF que abrió la persona
    guardados = sorted(p.name for p in (carpeta_datos / "fuentes_compania" / "CO-COD38").glob("*.pdf"))
    assert guardados == ["2025-09-30_Reporte-3T25-ES.pdf", "2025-12-31_Estados-Financieros-Consolidados-2025.pdf",
                         "2026-03-31_Reporte-1T26.pdf"]
    assert r["CO-COD38"]["guardados"] == 2 and r["CO-COD38"]["abiertos"] == 1
    assert not any("Separados" in d for d in nav.descargas)
    # Una segunda lectura no vuelve a bajar lo que ya está
    nav2 = Navegador()
    r2 = ev.leer(["CO-COD38"], abierta_s=0, navegador_factory=nav2, avisar=lambda m: None)
    assert r2["CO-COD38"]["guardados"] == 0 and r2["CO-COD38"]["ya_estaban"] == 2 and nav2.descargas == []


def test_solo_pdf_de_verdad(carpeta_datos):
    assert ev.guardar("CO-COT60", "https://x.co/a.pdf", b"<html>no</html>") is None
    assert ev.guardar("CO-COT60", "https://x.co/Estados-Consolidados-Junio-2026.pdf", PDF).name == \
        "2026-06-30_Estados-Consolidados-Junio-2026.pdf"


def test_emisores_marcados_en_la_configuracion():
    from invest.ingest import estados_compania as ec

    marcados = {e["emisor_id"] for e in ec.config()["emisores"] if e.get("ventana")}
    assert marcados == {"CO-COD38", "CO-COT60", "CO-COT04", "CO-COT09"}
    assert date(2026, 3, 31) == ec.corte_probable("https://argos.co/wp-content/uploads/2026/05/Reporte-1T26.pdf")
