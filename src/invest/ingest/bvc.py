"""Conector de la Bolsa de Colombia: cierre, apertura, máximo, mínimo, volumen y cantidad del día.

La SFC publica solo el precio promedio ponderado. La Bolsa de Colombia muestra en su página
"Mercado local en línea" una tabla con todas las acciones y ETF del día. Su API exige un token de
sesión que la web entrega a su propio sitio; ese token no se replica. En su lugar, un navegador
real (Playwright con Chromium, en el PC) abre la página como lo haría una persona, el propio sitio
hace su verificación y el programa lee la tabla ya mostrada. Sin captchas ni bloqueos que sortear;
si el sitio los pone, el conector falla y se avisa.

Reglas para no guardar datos de otro día:
- solo después de las 16:15 (hora de Bogotá; el mercado cierra a las 15:55 y la página tiene 15
  minutos de retraso) y nunca si la página todavía dice "Mercado abierto";
- solo si la fecha de la página ("HOY aaaa/mm/dd") es una sesión bursátil y es la de hoy.
Días perdidos (25-nov-2026): la misma página tiene un filtro por fecha (botón "Filtros", calendario y
"Filtrar") que muestra la tabla de un día pasado, con cierre, apertura, máximo, mínimo, volumen y
cantidad (el promedio sale "-"). En la misma ventana, después de la tabla de hoy, se piden los días
hábiles de las últimas ``DIAS_RECUPERAR`` sesiones que no estén en silver/fact_precio_bvc, del más
reciente al más viejo, con una pausa entre uno y otro. Un día se guarda solo si la tabla cambió después
de filtrar (la página sigue diciendo "HOY" con la fecha de hoy, así que el cambio es la única prueba).
Hay días que el servicio de la Bolsa no entrega nunca (responde 417: el 30-oct y el 19-nov de 2026,
probado varias veces, mientras el 4, 20 y 24-nov cargan): ese día se anota y se sigue con los demás.
Ante un 403 o 429 (rechazo) se detiene y no se reintenta. Hay dos diseños de la página: en escritorio un
selector de fecha (desde 2010) y en pantallas angostas el botón "Filtros"; se manejan los dos.

Precio en curso (``ConectorBVCEnCurso``, fuente ``bvc_en_curso``): la misma tabla leída durante la
sesión, con 15 minutos de retraso. Se guarda aparte (silver/fact_precio_bvc_en_curso, una fila por
día y nemotécnico, la última lectura) y nunca como cierre.

Requiere el extra ``pc``:  ``uv sync --extra pc``  y una vez  ``uv run playwright install chromium``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import yaml

from invest.calendario import dia_habil_anterior, es_habil
from invest import storage
from invest.config import CONFIG_DIR, data_dir
from invest.ingest.base import Conector

URL = "https://www.bvc.com.co/mercado-local-en-linea?tab=renta-variable_mercado-local"
URL_INDICES = "https://www.bvc.com.co/mercado-local-en-linea?tab=indices_accionarios"
BOGOTA = ZoneInfo("America/Bogota")
HORA_MINIMA = time(16, 15)

COLUMNAS = {
    "Nemotécnico": "nemotecnico", "Precios (último /cierre)": "cierre", "Variación porcentual": "variacion_pct",
    "Volúmenes": "volumen_cop", "Cantidad": "cantidad", "Variación absoluta": "variacion_abs",
    "Precio apertura": "apertura", "Precio máximo": "maximo", "Precio mínimo": "minimo",
    "Precio promedio": "promedio", "Emisor / Nombre": "emisor",
}
NUMERICAS = ["cierre", "variacion_pct", "volumen_cop", "cantidad", "variacion_abs", "apertura", "maximo", "minimo",
             "promedio"]

ESQUEMA = {
    "fecha": pl.Date, "nemotecnico": pl.Utf8, "especie": pl.Utf8, "clase": pl.Utf8, "emisor": pl.Utf8,
    **{c: pl.Float64 for c in NUMERICAS}, "fuente_id": pl.Utf8, "ingest_ts": pl.Datetime("us", "UTC"),
}

# Lee la tabla tal como la muestra la página (encabezados, filas, fecha y estado del mercado)
JS_TABLA = """() => {
  const t = document.querySelector('table');
  const hoja = e => e && e.childElementCount === 0;
  const hoy = [...document.querySelectorAll('*')].find(e => hoja(e) && e.textContent.trim() === 'HOY');
  const estado = [...document.querySelectorAll('*')].find(e => hoja(e) && /^Mercado (abierto|cerrado)/i.test(e.textContent.trim()));
  return {
    hoy: hoy ? hoy.parentElement.textContent.trim().slice(0, 40) : null,
    estado: estado ? estado.textContent.trim() : null,
    encabezado: t ? [...t.querySelectorAll('thead th')].map(th => th.textContent.trim()) : [],
    filas: t ? [...t.querySelectorAll('tbody tr')].map(tr => [...tr.querySelectorAll('td')].map(td => td.textContent.trim())) : [],
  };
}"""


DIAS_RECUPERAR = 10          # sesiones hacia atrás que se revisan en cada lectura manual
PAUSA_ENTRE_DIAS_S = 4       # entre un día pedido y el siguiente, para no pedir de corrido

# Elige un día en el calendario del filtro y espera a que la tabla cambie. Solo clics, como una persona.
JS_ELEGIR_FECHA = r"""async ([anio, mes, dia]) => {
  // Dos diseños de la misma página: en escritorio un selector de fecha (react-date-picker, desde 2010) con
  // botón de calendario que filtra al elegir el día; en pantallas angostas el botón "Filtros" abre el
  // calendario y luego "Filtrar". En los dos se elige el día con clics, como una persona.
  const MESES = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
  const esperar = ms => new Promise(r => setTimeout(r, ms));
  const visible = e => !!(e && e.offsetParent);
  const firma = () => [...document.querySelectorAll('table tbody tr')].slice(0, 40).map(tr => tr.textContent).join('|');
  const calendario = () => [...document.querySelectorAll('.react-calendar')].find(visible);
  const iso = `${anio}-${String(mes).padStart(2, '0')}-${String(dia).padStart(2, '0')}`;
  // Estado HTTP de la consulta de la tabla de ese día (la hace la propia página)
  const estadoConsulta = () => {
    const e = performance.getEntriesByType('resource').filter(x => x.name.includes('tradeDate]=' + iso)).pop();
    return e ? (e.responseStatus || 0) : null;
  };
  const antes = firma();
  if (!calendario()) {
    const abrir = [...document.querySelectorAll('.react-date-picker__calendar-button')].find(visible)
      || [...document.querySelectorAll('p, span, button')].find(e => e.textContent.trim() === 'Filtros' && visible(e));
    if (!abrir) return {ok: false, error: 'la página no tiene el selector de fecha'};
    abrir.click();
    for (let i = 0; i < 40 && !calendario(); i++) await esperar(250);
  }
  const cal = calendario();
  if (!cal) return {ok: false, error: 'el calendario no se abrió'};
  const objetivo = `${MESES[mes - 1]} - ${anio}`;
  const etiquetaDe = () => cal.querySelector('.react-calendar__navigation__label').textContent.trim().toLowerCase().replace(/\s+de\s+/, ' - ');
  for (let i = 0; i < 200; i++) {
    const etiqueta = etiquetaDe();
    if (etiqueta === objetivo) break;
    const m = MESES.findIndex(x => etiqueta.startsWith(x));
    const a = Number((etiqueta.match(/\d{4}/) || [0])[0]);
    if (m < 0 || !a) return {ok: false, error: `no se entiende el mes del calendario (${etiqueta})`};
    const boton = cal.querySelector(anio * 12 + mes - 1 < a * 12 + m ? '.react-calendar__navigation__prev-button' : '.react-calendar__navigation__next-button');
    if (!boton || boton.disabled) return {ok: false, error: `el calendario no llega a ${objetivo}`};
    boton.click(); await esperar(150);
  }
  const abbr = cal.querySelector(`abbr[aria-label="${dia} de ${MESES[mes - 1]} de ${anio}"]`);
  if (!abbr) return {ok: false, error: 'el día no está en el calendario'};
  const celda = abbr.closest('button');
  if (celda.disabled) return {ok: false, saltar: true, error: 'el calendario no deja elegir ese día'};
  celda.click(); await esperar(300);
  const filtrar = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Filtrar' && visible(b));
  if (filtrar) filtrar.click();
  // En escritorio el selector guarda la fecha elegida: si no es la pedida, no se lee nada
  const selector = [...document.querySelectorAll('.react-date-picker')].find(visible);
  const elegida = selector && selector.querySelector('input[name=date]');
  if (elegida && elegida.value && elegida.value !== iso) return {ok: false, error: `el selector quedó en ${elegida.value}`};
  for (let i = 0; i < 120; i++) {
    await esperar(250);
    const st = estadoConsulta();
    if (st && st >= 400) return {ok: false, saltar: st !== 403 && st !== 429, error: `la Bolsa no entregó ese día (HTTP ${st})`};
    const f = firma();
    if (f && f !== antes) { await esperar(1500); return {ok: true, cambio: true}; }
  }
  return {ok: true, cambio: false};
}"""


def equivalencias() -> dict[str, dict]:
    return yaml.safe_load((CONFIG_DIR / "nemotecnicos.yaml").read_text(encoding="utf-8"))["nemotecnicos"]


def _diagnostico(pagina, url: str) -> Path:
    """Guarda captura, texto y lo que pidió la página cuando la tabla no aparece (solo en invest-data)."""
    carpeta = data_dir() / "diagnostico"
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now(BOGOTA).strftime("%Y%m%d_%H%M%S")
    base = carpeta / f"bvc_{marca}"
    try:
        pagina.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:  # noqa: BLE001 - el diagnóstico no debe tapar el error original
        pass
    try:
        info = pagina.evaluate("""() => ({url: location.href, titulo: document.title, agente: navigator.userAgent,
            tablas: document.querySelectorAll('table').length, texto: document.body ? document.body.innerText.slice(0, 4000) : ''})""")
    except Exception as e:  # noqa: BLE001
        info = {"error": repr(e)}
    info["pedida"] = url
    base.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return base


class BloqueoBVC(RuntimeError):
    """La Bolsa de Colombia (su CDN) rechazó la lectura. No se reintenta: un bloqueo no se esquiva."""


ES_BLOQUEO_JS = """() => { const t = (document.title || '') + ' ' + (document.body ? document.body.innerText.slice(0, 600) : '');
    return /could not be satisfied|request blocked|access denied|403 error/i.test(t) }"""


def es_bloqueo(estado_http: int | None, titulo: str = "", texto: str = "") -> bool:
    """La respuesta es la página de rechazo del CDN (403 o su texto), no la de la Bolsa."""
    if estado_http in (401, 403, 429):
        return True
    return bool(re.search(r"could not be satisfied|request blocked|access denied|403 error", f"{titulo} {texto[:600]}", re.I))


def leer_pagina(url: str = URL, timeout_s: int = 90, visible: bool | None = None, fechas: tuple = ()) -> dict:
    """Abre la página en Chromium y devuelve lo que muestra.

    Con ``fechas`` (días pasados, del más reciente al más viejo) pide además cada uno con el filtro de
    la página y deja sus tablas en ``previos`` ({fecha ISO: tabla, o {"error": motivo}}); se detiene en
    el primer día que la Bolsa no entrega.

    La Bolsa de Colombia responde 403 a los navegadores sin ventana (bloqueo de su CDN). Por decisión de
    Esteban (23-nov-2026) no se esquiva: estos conectores son de lectura manual, se lanzan a mano y abren
    una ventana normal, como si él abriera la página; nunca van en una tarea programada.
    Si la página es el rechazo del CDN, se detiene de inmediato con ``BloqueoBVC`` (sin reintentar).
    Si la tabla no aparece por otra razón, deja captura y texto en ``invest-data/diagnostico/`` y repite
    el error.
    """
    from playwright.sync_api import TimeoutError as EsperaAgotada
    from playwright.sync_api import sync_playwright

    if visible is None:
        visible = os.environ.get("INVEST_NAVEGADOR_VISIBLE", "") in ("1", "si", "sí", "true")
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=not visible)
        try:
            pagina = navegador.new_page(locale="es-CO", viewport={"width": 1366, "height": 900})
            respuesta = pagina.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            if es_bloqueo(respuesta.status if respuesta else None, pagina.title()):
                base = _diagnostico(pagina, url)
                raise BloqueoBVC(f"la Bolsa rechazó la lectura (HTTP {respuesta.status if respuesta else '?'}); "
                                 f"no se reintenta. Vuelva a intentarlo más tarde; diagnóstico en {base.name}.png/.json")
            try:
                pagina.wait_for_function(
                    "() => { const t = document.querySelector('table'); return (t && t.querySelectorAll('tbody tr').length > 0)"
                    f" || ({ES_BLOQUEO_JS})() }}",
                    timeout=timeout_s * 1000)
            except EsperaAgotada as e:
                base = _diagnostico(pagina, url)
                raise TimeoutError(f"la tabla no apareció en {timeout_s} s; diagnóstico en {base.name}.png/.json") from e
            if pagina.evaluate(ES_BLOQUEO_JS):
                base = _diagnostico(pagina, url)
                raise BloqueoBVC(f"la Bolsa rechazó la lectura; no se reintenta. Diagnóstico en {base.name}.png/.json")
            pagina.wait_for_timeout(2000)  # deja terminar de pintar la tabla
            datos = pagina.evaluate(JS_TABLA)
            previos: dict[str, dict] = {}
            for f in fechas:
                pagina.wait_for_timeout(PAUSA_ENTRE_DIAS_S * 1000)
                r = pagina.evaluate(JS_ELEGIR_FECHA, [f.year, f.month, f.day])
                if not r.get("ok") or not r.get("cambio"):
                    previos[f.isoformat()] = {"error": r.get("error") or "la tabla no cambió: la Bolsa no entregó ese día"}
                    if r.get("saltar"):
                        continue  # ese día no existe en su servicio (417 siempre): se sigue con los demás
                    break
                previos[f.isoformat()] = pagina.evaluate(JS_TABLA)
            if fechas:
                datos["previos"] = previos
            return datos
        finally:
            navegador.close()


def numero(texto: str | None) -> float | None:
    """'49,345,537,805.00' → 49345537805.0; '1.08%' → 1.08; '-' → None (formato de EE. UU.)."""
    if texto is None:
        return None
    t = texto.replace("%", "").replace(",", "").replace("$", "").strip()
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fecha_pagina(hoy: str | None) -> date | None:
    """'HOY 2026/11/23' → date(2026, 11, 23)."""
    if not hoy:
        return None
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})", hoy)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


_bloqueo_previo: str | None = None  # rechazo del CDN visto en este proceso


def hay_consola() -> bool:
    """Hay una persona lanzando la lectura: una consola, o el botón del aplicativo local.

    El aplicativo marca ``INVEST_LECTURA_MANUAL=1`` solo en la lectura que lanza su botón (y su servidor
    rechaza pedidos de otras páginas). Una tarea programada o un workflow no tienen ninguna de las dos.
    """
    if os.environ.get("INVEST_LECTURA_MANUAL") == "1":
        return True
    return bool(sys.stdin) and sys.stdin.isatty()


def _leer_si_cerro(con: Conector, url: str) -> tuple[dict | None, date | None]:
    """Lee la página solo si ya cerró el mercado de hoy; si no, marca la corrida sin novedades."""
    ahora = (getattr(con, "_ahora", None) or datetime.now(timezone.utc)).astimezone(BOGOTA)
    if not es_habil(ahora.date()) or ahora.time() < HORA_MINIMA:
        con.sin_novedades = True
        con.notas.append("fuera del horario: se lee después de las 16:15 de un día bursátil")
        return None, None
    if not hay_consola():
        # Sin consola no hay una persona lanzándolo: no se abre la página
        raise RuntimeError("lectura manual: correr a mano desde PowerShell (uv run invest ingest bvc_web bvc_indices)")
    global _bloqueo_previo
    if _bloqueo_previo:
        # Si la Bolsa ya rechazó una lectura en esta misma corrida, no se pide otra página
        raise BloqueoBVC(f"no se lee: la Bolsa rechazó la lectura anterior de esta corrida ({_bloqueo_previo})")
    try:
        datos = leer_pagina(url, visible=True)
    except BloqueoBVC as e:
        _bloqueo_previo = str(e)[:80]
        raise
    fecha = fecha_pagina(datos.get("hoy"))
    if fecha != ahora.date():
        con.sin_novedades = True
        con.notas.append(f"la página muestra otra fecha ({fecha})")
        return None, None
    if "abierto" in (datos.get("estado") or "").lower():
        con.sin_novedades = True
        con.notas.append("la página todavía muestra el mercado abierto")
        return None, None
    return datos, fecha


def _filas(datos: dict, fecha: date) -> list[dict]:
    nombres = [COLUMNAS.get(h, h) for h in datos["encabezado"]]
    return [{"fecha": fecha, **dict(zip(nombres, fila))} for fila in datos["filas"] if fila]


class ConectorBVC(Conector):
    fuente_id = "bvc_web"
    tabla = "fact_precio_bvc"
    claves = ("fecha", "nemotecnico")

    def __init__(self, ahora: datetime | None = None) -> None:
        super().__init__()
        self._ahora = ahora  # para pruebas

    def faltantes(self, hoy: date) -> list[date]:
        """Sesiones de las últimas ``DIAS_RECUPERAR`` (sin hoy) que no están en silver/fact_precio_bvc."""
        try:
            previa = storage.leer_tabla("silver", self.tabla)
        except Exception:  # noqa: BLE001 - primera lectura
            previa = None
        tenidas = set() if previa is None or previa.is_empty() else set(previa.get_column("fecha").unique().to_list())
        dias, d = [], hoy
        while len(dias) < DIAS_RECUPERAR:
            d = dia_habil_anterior(d)
            dias.append(d)
        return [x for x in dias if x not in tenidas]

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        ahora = (self._ahora or datetime.now(timezone.utc)).astimezone(BOGOTA)
        hoy = ahora.date()
        leer_hoy = es_habil(hoy) and ahora.time() >= HORA_MINIMA
        previas = self.faltantes(hoy)
        if not leer_hoy and not previas:
            self.sin_novedades = True
            self.notas.append("fuera del horario: se lee después de las 16:15 de un día bursátil")
            return []
        if not hay_consola():
            raise RuntimeError("lectura manual: correr a mano desde PowerShell (uv run invest ingest bvc_web bvc_indices)")
        global _bloqueo_previo
        if _bloqueo_previo:
            raise BloqueoBVC(f"no se lee: la Bolsa rechazó la lectura anterior de esta corrida ({_bloqueo_previo})")
        try:
            datos = leer_pagina(URL, visible=True, fechas=tuple(previas))
        except BloqueoBVC as e:
            _bloqueo_previo = str(e)[:80]
            raise
        faltan = [c for c in COLUMNAS if c not in datos.get("encabezado", [])]
        if faltan:
            raise ValueError(f"La tabla de la Bolsa de Colombia cambió: faltan las columnas {faltan}")
        self.crudos["mercado_local.json"] = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        registros: list[dict] = []
        if leer_hoy:
            fecha = fecha_pagina(datos.get("hoy"))
            if fecha != hoy:
                self.notas.append(f"la página muestra otra fecha ({fecha})")
            elif "abierto" in (datos.get("estado") or "").lower():
                self.notas.append("la página todavía muestra el mercado abierto")
            else:
                registros += _filas(datos, fecha)
        else:
            self.notas.append("hoy fuera del horario (después de las 16:15): solo días anteriores")
        recuperados = []
        for f_iso, tabla in (datos.get("previos") or {}).items():
            if "error" in tabla:
                self.notas.append(f"{f_iso} no se recuperó: {tabla['error']}")
                continue
            if [c for c in COLUMNAS if c not in tabla.get("encabezado", [])]:
                self.notas.append(f"{f_iso} no se recuperó: la tabla filtrada cambió de columnas")
                continue
            registros += _filas(tabla, date.fromisoformat(f_iso))
            recuperados.append(f_iso)
        if recuperados:
            self.notas.append("días recuperados: " + ", ".join(sorted(recuperados)))
        if not registros:
            self.sin_novedades = True
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        if not registros:
            return pl.DataFrame(schema=ESQUEMA)
        eq = equivalencias()
        ahora = datetime.now(timezone.utc)
        sin_equivalencia = sorted({r["nemotecnico"] for r in registros if r["nemotecnico"] not in eq})
        if sin_equivalencia:
            self.notas.append("nemotécnicos sin equivalencia SFC: " + ", ".join(sin_equivalencia))
        filas = []
        for r in registros:
            e = eq.get(r["nemotecnico"], {})
            filas.append({
                "fecha": r["fecha"], "nemotecnico": r["nemotecnico"], "especie": e.get("especie"),
                "clase": e.get("clase"), "emisor": r.get("emisor"),
                **{c: numero(r.get(c)) for c in NUMERICAS}, "fuente_id": self.fuente_id, "ingest_ts": ahora,
            })
        return pl.DataFrame(filas, schema=ESQUEMA)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        motivo = (
            pl.when(pl.col("maximo") < pl.col("minimo")).then(pl.lit("máximo menor que mínimo"))
            .when((pl.col("cierre") > pl.col("maximo") * 1.0001) | (pl.col("cierre") < pl.col("minimo") * 0.9999))
            .then(pl.lit("cierre fuera del rango del día"))
            .when(pl.col("cierre") <= 0).then(pl.lit("cierre no positivo"))
            .otherwise(pl.lit(None))
        )
        df = df.with_columns(motivo.alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())


# --------------------------------------------------------------------------- precio en curso
ESQUEMA_EN_CURSO = {**ESQUEMA, "hora_lectura": pl.Datetime("us"), "estado_mercado": pl.Utf8}


class ConectorBVCEnCurso(ConectorBVC):
    """La tabla de hoy leída durante la sesión (15 minutos de retraso), aparte del cierre.

    Una fila por día y nemotécnico con la última lectura (``hora_lectura``, hora de Bogotá) y el estado
    que mostraba la página. El aplicativo la usa solo mientras no haya un cierre de ese día.
    """

    fuente_id = "bvc_en_curso"
    tabla = "fact_precio_bvc_en_curso"
    claves = ("fecha", "nemotecnico")

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        ahora = (self._ahora or datetime.now(timezone.utc)).astimezone(BOGOTA)
        if not es_habil(ahora.date()):
            self.sin_novedades = True
            self.notas.append("hoy no es día bursátil")
            return []
        if not hay_consola():
            raise RuntimeError("lectura manual: se lanza desde el aplicativo o PowerShell (uv run invest ingest bvc_en_curso)")
        global _bloqueo_previo
        if _bloqueo_previo:
            raise BloqueoBVC(f"no se lee: la Bolsa rechazó la lectura anterior de esta corrida ({_bloqueo_previo})")
        try:
            datos = leer_pagina(URL, visible=True)
        except BloqueoBVC as e:
            _bloqueo_previo = str(e)[:80]
            raise
        faltan = [c for c in COLUMNAS if c not in datos.get("encabezado", [])]
        if faltan:
            raise ValueError(f"La tabla de la Bolsa de Colombia cambió: faltan las columnas {faltan}")
        fecha = fecha_pagina(datos.get("hoy"))
        if fecha != ahora.date():
            self.sin_novedades = True
            self.notas.append(f"la página muestra otra fecha ({fecha}): el mercado de hoy no ha abierto")
            return []
        self.crudos["en_curso.json"] = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        hora = ahora.replace(tzinfo=None, microsecond=0)
        estado = datos.get("estado")
        self.notas.append(f"lectura de las {hora:%H:%M} ({estado or 'estado sin leer'})")
        return [{**r, "hora_lectura": hora, "estado_mercado": estado} for r in _filas(datos, fecha)]

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        base = super().interpretar(registros)
        if base.is_empty():
            return pl.DataFrame(schema=ESQUEMA_EN_CURSO)
        extra = pl.DataFrame([{"hora_lectura": r["hora_lectura"], "estado_mercado": r["estado_mercado"]} for r in registros],
                             schema={"hora_lectura": pl.Datetime("us"), "estado_mercado": pl.Utf8})
        return pl.concat([base, extra], how="horizontal").select(list(ESQUEMA_EN_CURSO)).cast(ESQUEMA_EN_CURSO)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        # Durante la sesión el último precio puede quedar por fuera del rango si la página no ha refrescado
        # máximo y mínimo: solo se descarta lo imposible
        motivo = (pl.when(pl.col("cierre") <= 0).then(pl.lit("precio no positivo"))
                  .when(pl.col("maximo") < pl.col("minimo")).then(pl.lit("máximo menor que mínimo"))
                  .otherwise(pl.lit(None)))
        df = df.with_columns(motivo.alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())


# --------------------------------------------------------------------------- índices
ENCABEZADO_INDICES = ["Índices", "Valor hoy", "Valor ayer", "Variación absoluta", "Variación porcentual"]


class ConectorBVCIndices(Conector):
    """MSCI COLCAP y MSCI NUAM USD de la pestaña de índices de la misma página.

    Guarda el valor de hoy y el de ayer (la sesión anterior): si el PC estuvo apagado un día, la
    corrida siguiente recupera ese cierre. Van a silver/fact_serie_macro con serie_id
    ``msci_colcap`` y ``msci_nuam_usd``.
    """

    fuente_id = "bvc_indices"
    # Una carpeta por fuente dentro de la tabla: así GitHub Actions y el PC nunca escriben el mismo
    # archivo (leer_tabla("silver", "fact_serie_macro") las une)
    tabla = "fact_serie_macro/fuente=bvc_indices"
    claves = ("fecha", "serie_id")

    def __init__(self, ahora: datetime | None = None) -> None:
        super().__init__()
        self._ahora = ahora

    def descargar(self, desde: date, hasta: date) -> list[dict]:
        datos, fecha = _leer_si_cerro(self, URL_INDICES)
        if datos is None:
            return []
        faltan = [c for c in ENCABEZADO_INDICES if c not in datos.get("encabezado", [])]
        if faltan:
            raise ValueError(f"La tabla de índices de la Bolsa de Colombia cambió: faltan {faltan}")
        self.crudos["indices.json"] = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        i = {h: datos["encabezado"].index(h) for h in ENCABEZADO_INDICES}
        anterior = dia_habil_anterior(fecha)
        registros = []
        for fila in datos["filas"]:
            serie = re.sub(r"[^a-z0-9]+", "_", fila[i["Índices"]].lower()).strip("_")
            registros.append({"fecha": fecha, "serie_id": serie, "valor": numero(fila[i["Valor hoy"]])})
            registros.append({"fecha": anterior, "serie_id": serie, "valor": numero(fila[i["Valor ayer"]])})
        return registros

    def interpretar(self, registros: list[dict]) -> pl.DataFrame:
        from invest.ingest.dane_ipc import ESQUEMA as ESQUEMA_MACRO

        ahora = datetime.now(timezone.utc)
        return pl.DataFrame([{
            **r, "unidad": "puntos", "frecuencia": "diaria", "fecha_publicacion": None,
            "publicacion_estimada": None, "fuente_id": self.fuente_id, "ingest_ts": ahora,
        } for r in registros if r["valor"] is not None], schema=ESQUEMA_MACRO)

    def validar(self, df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        df = df.with_columns(pl.when(pl.col("valor") <= 0).then(pl.lit("valor no positivo")).otherwise(pl.lit(None))
                             .alias("motivo"))
        return df.filter(pl.col("motivo").is_null()).drop("motivo"), df.filter(pl.col("motivo").is_not_null())
