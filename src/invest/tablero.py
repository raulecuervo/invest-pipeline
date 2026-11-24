"""Tablero local de la matriz de factores: un solo archivo HTML, sin conexión a internet.

``invest tablero`` lee las tablas gold de ``invest.ficha`` y de la validación de factores y escribe
``invest-data/tableros/matriz_factores.html`` (no se versiona: se regenera cada día en el PC). Los
datos van incrustados en la página; no se cargan librerías ni fuentes de afuera, y nada sale del
computador. Es una herramienta educativa: ordena acciones según factores validados con datos
históricos y no emite señales de compra o venta.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path

import polars as pl

from invest import storage
from invest.config import cargar_yaml
from invest.factores import config as config_factores

NOMBRES_FACTOR = {
    "momentum_12_1": "Momentum 12-1",
    "momentum_6_1": "Momentum 6-1",
    "reversion_1m": "Reversión de 1 mes",
    "volatilidad_1a": "Baja volatilidad",
    "liquidez_60": "Prima por iliquidez",
    "rendimiento_dividendo_12m": "Rendimiento por dividendo 12m",
    "valor_libros_precio": "Valor en libros / precio",
    "combinado_validados": "Puntaje combinado",
}


def nombre_corto(especie: str, nombre: str | None, clase: str | None, cortos: dict[str, str]) -> str:
    base = cortos.get(especie[:5])
    if not base:
        n = (nombre or especie).upper()
        n = re.sub(r"^(AO|ADP|PEC|SIN DERECHO A VOTO)\s+", "", n)
        n = re.sub(r"^(PEC|SIN DERECHO A VOTO)\s+", "", n)
        n = re.split(r"\s+S\.\s?A|\s+\(|\s+-\s+|\s+ANTES|\s+E\.S\.P|\s+ESP\b", n)[0]
        base = n.title()
    return f"{base} (pref.)" if (clase or "").startswith("pref") else base


def _num(x):
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return round(x, 6) if isinstance(x, float) else x


def _fecha(x) -> str | None:
    return x.isoformat() if isinstance(x, (date, datetime)) else None


def datos() -> dict | None:
    """Arma el contenido del tablero desde gold; None si faltan las tablas."""
    cfg_t = cargar_yaml("tablero.yaml")
    cortos = cfg_t.get("nombres_cortos") or {}
    meses_detalle = int(cfg_t.get("meses_detalle", 36))
    cfg_f = config_factores()
    ficha = storage.leer_tabla("gold", "fact_ficha_accion")
    puntajes = storage.leer_tabla("gold", "fact_puntaje_mensual")
    historia = storage.leer_tabla("gold", "fact_puntaje_tercio")
    validacion = storage.leer_tabla("gold", "fact_validacion_factor")
    factores = storage.leer_tabla("gold", "fact_factor_mensual")
    if ficha is None or ficha.is_empty() or validacion is None or puntajes is None:
        return None
    fecha = ficha.get_column("fecha").max()

    filas_factor = []
    for r in validacion.iter_rows(named=True):
        f = r["factor"]
        filas_factor.append({
            "factor": f, "nombre": NOMBRES_FACTOR.get(f, f),
            "descripcion": (cfg_f["factores"].get(f) or {}).get("descripcion",
                                                               "Promedio de los percentiles de los factores validados"),
            "veredicto": r["veredicto"], "meses": _num(r.get("meses")),
            "ic": _num(r.get("ic_medio")), "ic_t": _num(r.get("ic_t")),
            "ic_desc": _num(r.get("ic_medio_descubrimiento")), "ic_fuera": _num(r.get("ic_medio_fuera_muestra")),
            "p_holm": _num(r.get("ic_p_holm")),
            "exceso": _num(r.get("exceso_superior")), "exceso_neto": _num(r.get("exceso_superior_neto")),
            "exceso_neto_t": _num(r.get("exceso_superior_neto_t")), "rotacion": _num(r.get("rotacion_superior")),
        })

    # Historia por acción: percentil del puntaje y retorno del mes (índice limpio), últimos N meses
    fechas = sorted(puntajes.get_column("fecha").unique().to_list())[-meses_detalle:]
    pp = (puntajes.filter(pl.col("fecha").is_in(fechas))
          .with_columns(((pl.col("acciones_mes") - pl.col("posicion") + 0.5) / pl.col("acciones_mes")).alias("pp")))
    ret = pl.DataFrame(schema={"fecha": pl.Date, "especie": pl.Utf8, "r": pl.Float64})
    if factores is not None:
        ret = (factores.filter((pl.col("factor") == "reversion_1m") & pl.col("fecha").is_in(fechas))
               .select("fecha", "especie", pl.col("valor").alias("r")))
    detalle = (pp.select("fecha", "especie", "pp", "tercio").join(ret, on=["fecha", "especie"], how="full",
                                                                  coalesce=True).sort("fecha"))
    por_especie: dict[str, list] = {}
    for r in detalle.iter_rows(named=True):
        por_especie.setdefault(r["especie"], []).append([_fecha(r["fecha"]), _num(r["pp"]), _num(r["r"]),
                                                         r["tercio"]])

    acciones, fuera = [], []
    for r in ficha.iter_rows(named=True):
        corto = nombre_corto(r["especie"], r["nombre"], r["clase_accion"], cortos)
        if r["con_puntaje"]:
            acciones.append({
                "especie": r["especie"], "nombre": r["nombre"], "corto": corto, "clase": r["clase_accion"],
                "posicion": r["posicion"], "tercio": r["tercio"], "n": r["acciones_mes"],
                "puntaje": _num(r["puntaje"]), "pp": _num(r["percentil_puntaje"]),
                "pp1": _num(r["percentil_puntaje_1m"]), "pp3": _num(r["percentil_puntaje_3m"]),
                "seguidos": r["meses_seguidos_superior"],
                "mom": _num(r.get("momentum_12_1_valor")), "mom_p": _num(r.get("momentum_12_1_percentil")),
                "div": _num(r.get("rendimiento_dividendo_12m_valor")),
                "div_p": _num(r.get("rendimiento_dividendo_12m_percentil")),
                "r12": _num(r.get("retorno_12m")), "r1": _num(r.get("retorno_1m")),
                "vol": _num(r.get("volatilidad_1a_valor")), "monto60": _num(r.get("monto_medio_60")),
                "frac60": _num(r.get("fraccion_dias_60")), "pvl": _num(r.get("precio_valor_libros")),
                "pu": _num(r.get("precio_utilidad")), "precio": _num(r.get("precio")),
                "ultimo": _fecha(r.get("fecha_ultimo_negocio")),
                "corte_vpa": _fecha(r.get("corte_valor_patrimonial")),
                "alertas": [a for a in (r.get("alertas") or "").split("; ") if a],
                "historia": por_especie.get(r["especie"], []),
            })
        else:
            fuera.append({"corto": corto, "nombre": r["nombre"], "motivo": r["motivo_fuera"] or "",
                          "ultimo": _fecha(r.get("fecha_ultimo_negocio")), "frac60": _num(r.get("fraccion_dias_60")),
                          "monto60": _num(r.get("monto_medio_60"))})

    serie = []
    if historia is not None and not historia.is_empty():
        serie = [[_fecha(r["fecha"]), _num(r["indice_superior"]), _num(r["indice_superior_neto"]),
                  _num(r["indice_universo"]), r["acciones"]] for r in historia.sort("fecha").iter_rows(named=True)]
    v = cfg_f["validacion"]
    return {
        "generado": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fecha": _fecha(fecha),
        "validados": [NOMBRES_FACTOR.get(f["factor"], f["factor"]) for f in filas_factor if f["veredicto"] == "validado"],
        "factores": filas_factor,
        "acciones": acciones, "fuera": fuera, "serie": serie,
        "parametros": {"costo_ida": v.get("costo_ida"), "inicio": str(v["inicio"]),
                       "fuera_muestra": str(v["inicio_fuera_de_muestra"]), "cuantiles": v["cuantiles"]},
    }


def escribir(destino: Path | None = None) -> Path | None:
    contenido = datos()
    if contenido is None:
        return None
    destino = destino or storage.data_dir() / "tableros" / "matriz_factores.html"
    destino.parent.mkdir(parents=True, exist_ok=True)
    carga = json.dumps(contenido, ensure_ascii=False).replace("</", "<\\/")
    destino.write_text(PLANTILLA.replace("/*__DATOS__*/null", carga), encoding="utf-8")
    return destino


PLANTILLA = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Matriz de factores · Bolsa de Colombia</title>
<style>
.viz-root{color-scheme:light;
 --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
 --ring:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;
 --t-inf:#86b6ef;--t-med:#3987e5;--t-sup:#184f95;--seq:#2a78d6;--seq-track:#e8eefa;
 --good:#006300;--bad:#d03b3b;--warn:#fab219;--chip:#f0efec}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
 --ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;
 --t-inf:#184f95;--t-med:#2a78d6;--t-sup:#86b6ef;--seq:#3987e5;--seq-track:#25303f;
 --good:#0ca30c;--bad:#e66767;--warn:#fab219;--chip:#2c2c2a}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
 --ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;
 --t-inf:#184f95;--t-med:#2a78d6;--t-sup:#86b6ef;--seq:#3987e5;--seq-track:#25303f;
 --good:#0ca30c;--bad:#e66767;--warn:#fab219;--chip:#2c2c2a}
*{box-sizing:border-box}
body{margin:0}
.viz-root{background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
 min-height:100vh;padding:24px clamp(12px,3vw,40px) 48px}
h1{font-size:22px;margin:0 0 4px;font-weight:650}
h2{font-size:16px;margin:0 0 4px;font-weight:650}
.sub{color:var(--ink2);margin:0}
.aviso{margin:14px 0 20px;padding:10px 14px;border-radius:8px;background:var(--surface);box-shadow:0 0 0 1px var(--ring);
 color:var(--ink2)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--surface);border-radius:10px;box-shadow:0 0 0 1px var(--ring);padding:12px 14px}
.tile .v{font-size:24px;font-weight:650;margin-top:2px}
.tile .l{color:var(--ink2);font-size:12.5px}
.card{background:var(--surface);border-radius:12px;box-shadow:0 0 0 1px var(--ring);padding:16px 18px;margin-bottom:20px}
.card .desc{color:var(--ink2);margin:0 0 12px;max-width:80ch}
.fila2{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,.9fr);gap:20px}
@media (max-width:1000px){.fila2{grid-template-columns:1fr}}
svg text{fill:var(--muted);font-size:11px}
svg .lbl{fill:var(--ink2);font-size:11.5px}
svg .lbl-strong{fill:var(--ink);font-size:11.5px;font-weight:600}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;text-align:right;white-space:nowrap}
th{font-weight:600;color:var(--ink2);font-size:12px;border-bottom:1px solid var(--axis);cursor:pointer;user-select:none;
 position:sticky;top:0;background:var(--surface)}
th.izq,td.izq{text-align:left}
td{border-bottom:1px solid var(--grid)}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--chip)}
tbody tr.sel{background:var(--chip);box-shadow:inset 3px 0 0 var(--seq)}
.tabla-scroll{overflow:auto;max-height:640px}
.tabla-ancha{overflow-x:auto}
.bar{display:inline-block;width:70px;height:8px;border-radius:4px;background:var(--seq-track);vertical-align:middle;
 margin-left:6px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--seq);border-radius:4px}
.chip{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:999px;font-size:12px;
 background:var(--chip);color:var(--ink)}
.chip .dot{width:9px;height:9px;border-radius:50%}
.up{color:var(--good)} .down{color:var(--bad)}
.alerta{color:var(--ink2);font-size:12px}
.alerta::before{content:"⚠ ";color:var(--warn)}
.muted{color:var(--muted)}
.leyenda{display:flex;gap:14px;flex-wrap:wrap;color:var(--ink2);font-size:12.5px;margin:6px 0 0}
.leyenda span{display:inline-flex;align-items:center;gap:6px}
.leyenda i{width:14px;height:3px;border-radius:2px;display:inline-block}
.leyenda i.c{width:10px;height:10px;border-radius:50%}
#tip{position:fixed;pointer-events:none;z-index:9;background:var(--surface);color:var(--ink);border-radius:8px;
 box-shadow:0 0 0 1px var(--ring),0 6px 18px rgba(0,0,0,.18);padding:8px 10px;font-size:12.5px;display:none;max-width:280px}
#tip b{font-weight:650}
#tip .r{display:flex;justify-content:space-between;gap:14px}
#tip .r span:first-child{color:var(--ink2)}
.detalle-vacio{color:var(--muted);padding:40px 0;text-align:center}
.det-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px 16px;margin:10px 0 14px}
.det-grid div span{display:block;color:var(--ink2);font-size:12px}
.det-grid div b{font-weight:600}
.veredicto{font-size:12px;padding:1px 8px;border-radius:999px;background:var(--chip)}
.veredicto.validado::before{content:"✓ ";color:var(--good);font-weight:700}
.veredicto.no_validado::before{content:"✕ ";color:var(--muted)}
.veredicto.debil::before{content:"~ ";color:var(--warn)}
.veredicto.informativo::before{content:"ⓘ ";color:var(--ink2)}
footer{color:var(--muted);font-size:12px;margin-top:8px;max-width:100ch}
</style>
</head>
<body>
<div class="viz-root">
<header>
 <h1>Matriz de factores · acciones de la Bolsa de Colombia</h1>
 <p class="sub" id="sub"></p>
</header>
<p class="aviso">Herramienta educativa. Ordena las acciones líquidas según los factores que pasaron una validación
estadística con datos históricos; no es una recomendación de compra o venta, no considera su situación ni sus impuestos,
y un patrón del pasado puede no repetirse.</p>
<section class="tiles" id="tiles"></section>

<section class="fila2">
 <div class="card">
  <h2>Matriz de los factores validados</h2>
  <p class="desc" id="desc-matriz"></p>
  <div id="matriz"></div>
  <div class="leyenda" id="ley-matriz"></div>
 </div>
 <div class="card" id="detalle"><div class="detalle-vacio">Elija una acción en la matriz o en la tabla.</div></div>
</section>

<section class="card">
 <h2>Ficha de cada acción del universo</h2>
 <p class="desc">El puntaje es el promedio de los percentiles de los factores validados (100 = lo más favorable según la
 hipótesis de cada uno). Las columnas de la derecha son contexto: no entran en el puntaje. Clic en un encabezado para ordenar.</p>
 <div class="tabla-scroll"><table id="tabla"></table></div>
</section>

<section class="card">
 <h2>¿Qué tanto ha funcionado? Tercio superior del puntaje frente al universo</h2>
 <p class="desc" id="desc-hist"></p>
 <div id="historia"></div>
 <div class="leyenda" id="ley-hist"></div>
</section>

<section class="card">
 <h2>Evidencia de cada factor</h2>
 <p class="desc" id="desc-evid"></p>
 <div class="tabla-ancha"><table id="evidencia"></table></div>
</section>

<section class="card">
 <h2>Acciones fuera del universo</h2>
 <p class="desc">Acciones activas que este mes no cumplen las reglas del universo (liquidez, negocios recientes o historia
 suficiente). No reciben puntaje porque sus precios no permiten medir los factores con confianza.</p>
 <div class="tabla-ancha"><table id="fuera"></table></div>
</section>
<footer id="pie"></footer>
<div id="tip" role="status"></div>
</div>
<script>
const D = /*__DATOS__*/null;
const NS = "http://www.w3.org/2000/svg";
const $ = s => document.querySelector(s);
const pct = (x, d = 1) => x == null ? "—" : (x * 100).toLocaleString("es-CO", {minimumFractionDigits: d, maximumFractionDigits: d}) + " %";
const num = (x, d = 2) => x == null ? "—" : x.toLocaleString("es-CO", {minimumFractionDigits: d, maximumFractionDigits: d});
const p100 = x => x == null ? "—" : Math.round(x * 100).toString();
const mm = x => x == null ? "—" : (x / 1e6).toLocaleString("es-CO", {maximumFractionDigits: 0}) + " M";
const MESES = ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"];
const fmes = s => { if (!s) return "—"; const [y, m, d] = s.split("-"); return `${+d}-${MESES[+m - 1]}-${y}`; };
const fmesc = s => { const [y, m] = s.split("-"); return `${MESES[+m - 1]} ${y.slice(2)}`; };
const TERCIO = {superior: "--t-sup", medio: "--t-med", inferior: "--t-inf"};
const TNOM = {superior: "Tercio superior", medio: "Tercio medio", inferior: "Tercio inferior"};

function el(tag, attrs = {}, text) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
}
function sv(tag, attrs = {}, text) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
}
const tip = $("#tip");
function mostrarTip(ev, titulo, filas) {
  tip.replaceChildren();
  tip.append(el("b", {}, titulo));
  for (const [a, b] of filas) { const r = el("div", {class: "r"}); r.append(el("span", {}, a), el("span", {}, b)); tip.append(r); }
  tip.style.display = "block";
  const x = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8);
  const y = Math.min(ev.clientY + 14, innerHeight - tip.offsetHeight - 8);
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const ocultarTip = () => { tip.style.display = "none"; };

if (!D) { document.body.textContent = "Sin datos: corra invest ficha."; throw new Error("sin datos"); }
const A = D.acciones;
const comb = D.factores.find(f => f.factor === "combinado_validados");
let seleccion = null;

// ---------------------------------------------------------------- encabezado y cifras
$("#sub").textContent = `Datos al ${fmes(D.fecha)} · ${A.length} acciones con puntaje · generado ${D.generado}`;
const tiles = $("#tiles");
const tile = (v, l) => { const t = el("div", {class: "tile"}); t.append(el("div", {class: "l"}, l), el("div", {class: "v"}, v)); tiles.append(t); };
tile(`${D.validados.length} de ${D.factores.filter(f => f.veredicto !== "informativo").length}`, "factores validados");
tile(String(A.length), "acciones en el universo este mes");
if (comb) {
  tile(pct(comb.exceso_neto, 2), "exceso mensual histórico del tercio superior, neto de costos");
  tile(pct(comb.rotacion, 0), "del tercio superior cambia cada mes");
}
$("#desc-matriz").textContent = `Cada punto es una acción: a la derecha, mayor ${D.validados[0] || ""}; arriba, mayor ${D.validados[1] || ""} (percentiles dentro del universo del mes). El color es el tercio del puntaje combinado.`;

// ---------------------------------------------------------------- matriz (dispersión)
function matriz() {
  const caja = $("#matriz"); caja.replaceChildren();
  const W = Math.max(caja.clientWidth, 360), H = Math.min(520, Math.max(360, W * 0.72));
  const m = {l: 44, r: 16, t: 14, b: 40};
  const x = v => m.l + v * (W - m.l - m.r), y = v => H - m.b - v * (H - m.t - m.b);
  const s = sv("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
                       "aria-label": "Matriz de dispersión de los dos factores validados"});
  for (const t of [0, .25, .5, .75, 1]) {
    s.append(sv("line", {x1: x(t), x2: x(t), y1: y(0), y2: y(1), stroke: t === .5 ? "var(--axis)" : "var(--grid)", "stroke-dasharray": t === .5 ? "4 3" : null}));
    s.append(sv("line", {y1: y(t), y2: y(t), x1: x(0), x2: x(1), stroke: t === .5 ? "var(--axis)" : "var(--grid)", "stroke-dasharray": t === .5 ? "4 3" : null}));
    s.append(sv("text", {x: x(t), y: H - m.b + 16, "text-anchor": "middle"}, String(t * 100)));
    s.append(sv("text", {x: m.l - 8, y: y(t) + 4, "text-anchor": "end"}, String(t * 100)));
  }
  s.append(sv("text", {x: (x(0) + x(1)) / 2, y: H - 6, "text-anchor": "middle", class: "lbl"}, `${D.validados[0] || ""} (percentil)`));
  const ty = sv("text", {x: 12, y: (y(0) + y(1)) / 2, "text-anchor": "middle", class: "lbl", transform: `rotate(-90 12 ${(y(0) + y(1)) / 2})`}, `${D.validados[1] || ""} (percentil)`);
  s.append(ty);
  // Etiquetas de cuadrante, discretas
  // Puntos; etiquetas a la derecha con un empuje simple para que no se monten
  const pts = A.filter(a => a.mom_p != null && a.div_p != null).map(a => ({a, cx: x(a.mom_p), cy: y(a.div_p)}));
  // Nombre de cada cuadrante en su esquina, salvo que haya un punto debajo
  const q = [[.98, .98, "end", "altos en ambos"], [.02, .98, "start", "solo dividendo alto"], [.98, .03, "end", "solo momentum alto"], [.02, .03, "start", "bajos en ambos"]];
  for (const [qx, qy, a, t] of q) {
    const ty = y(qy) + (qy > .5 ? 10 : -4), ancho = t.length * 6, tx0 = a === "end" ? x(qx) - ancho : x(qx);
    if (pts.some(p => p.cx > tx0 - 120 && p.cx < tx0 + ancho + 8 && Math.abs(p.cy - ty) < 16)) continue;
    s.append(sv("text", {x: x(qx), y: ty, "text-anchor": a}, t));
  }
  const ordenY = [...pts].sort((p, q) => p.cy - q.cy);
  const ocupadas = [];
  for (const p of ordenY) {
    const ancho = p.a.corto.length * 6.6 + (seleccion === p.a.especie ? 6 : 0);
    let ly = p.cy + 4, lado = p.cx + 10 + ancho > W - m.r ? -1 : 1;
    const sep = seleccion === p.a.especie ? 15 : 9;
    const lx = lado > 0 ? p.cx + sep : p.cx - sep;
    const x0 = lado > 0 ? lx : lx - ancho, x1 = lado > 0 ? lx + ancho : lx;
    for (let i = 0; i < 12; i++) {
      const choca = ocupadas.some(o => o.x0 < x1 && o.x1 > x0 && Math.abs(o.y - ly) < 12);
      if (!choca) break;
      ly += 12;
    }
    ocupadas.push({x0, x1, y: ly});
    p.lx = lx; p.ly = ly; p.anchor = lado > 0 ? "start" : "end";
  }
  for (const p of pts) {
    const g = sv("g", {tabindex: 0, style: "cursor:pointer"});
    const sel = seleccion === p.a.especie;
    g.append(sv("circle", {cx: p.cx, cy: p.cy, r: 14, fill: "transparent"}));
    g.append(sv("circle", {cx: p.cx, cy: p.cy, r: sel ? 7.5 : 6, fill: `var(${TERCIO[p.a.tercio]})`, stroke: "var(--surface)", "stroke-width": 2}));
    if (sel) g.append(sv("circle", {cx: p.cx, cy: p.cy, r: 11, fill: "none", stroke: "var(--ink)", "stroke-width": 1.5}));
    g.append(sv("text", {x: p.lx, y: p.ly, "text-anchor": p.anchor, class: sel ? "lbl-strong" : "lbl"}, p.a.corto));
    const filas = () => [["Puntaje", p100(p.a.puntaje)], ["Posición", `${p.a.posicion} de ${p.a.n}`], [D.validados[0], p100(p.a.mom_p)], [D.validados[1], p100(p.a.div_p)]];
    g.addEventListener("pointermove", ev => mostrarTip(ev, p.a.corto, filas()));
    g.addEventListener("pointerleave", ocultarTip);
    g.addEventListener("focus", ev => { const r = g.getBoundingClientRect(); mostrarTip({clientX: r.right, clientY: r.top}, p.a.corto, filas()); });
    g.addEventListener("blur", ocultarTip);
    g.addEventListener("click", () => elegir(p.a.especie));
    g.addEventListener("keydown", ev => { if (ev.key === "Enter") elegir(p.a.especie); });
    s.append(g);
  }
  caja.append(s);
  const ley = $("#ley-matriz"); ley.replaceChildren();
  for (const t of ["superior", "medio", "inferior"]) {
    const sp = el("span"); const i = el("i", {class: "c"}); i.style.background = `var(${TERCIO[t]})`;
    sp.append(i, document.createTextNode(TNOM[t])); ley.append(sp);
  }
}

// ---------------------------------------------------------------- tabla de la ficha
const COLS = [
  {k: "posicion", t: "#", f: a => String(a.posicion)},
  {k: "corto", t: "Acción", izq: true, f: a => a.corto},
  {k: "puntaje", t: "Puntaje", f: a => p100(a.puntaje), bar: a => a.puntaje},
  {k: "tercio", t: "Tercio", izq: true, chip: true},
  {k: "cambio", t: "vs. mes ant.", v: a => a.pp1 == null ? null : a.pp - a.pp1, delta: true},
  {k: "seguidos", t: "Meses en superior", f: a => a.seguidos ? String(a.seguidos) : "—"},
  {k: "mom", t: "Momentum 12-1", f: a => pct(a.mom)},
  {k: "mom_p", t: "Percentil", f: a => p100(a.mom_p)},
  {k: "div", t: "Dividendo 12m", f: a => pct(a.div)},
  {k: "div_p", t: "Percentil", f: a => p100(a.div_p)},
  {k: "r12", t: "Retorno 12m", f: a => pct(a.r12)},
  {k: "vol", t: "Volatilidad", f: a => pct(a.vol, 0)},
  {k: "monto60", t: "Negociado/día", f: a => mm(a.monto60)},
  {k: "pvl", t: "P/VL", f: a => num(a.pvl)},
  {k: "pu", t: "P/U", f: a => num(a.pu, 1)},
  {k: "alertas", t: "Avisos", izq: true},
];
let orden = {k: "posicion", asc: true};
function tabla() {
  const t = $("#tabla"); t.replaceChildren();
  const th = el("thead"); const tr = el("tr");
  for (const c of COLS) {
    const h = el("th", {class: c.izq ? "izq" : "", scope: "col"}, c.t + (orden.k === c.k ? (orden.asc ? " ▲" : " ▼") : ""));
    h.addEventListener("click", () => { orden = {k: c.k, asc: orden.k === c.k ? !orden.asc : c.k === "posicion" || c.k === "corto"}; tabla(); });
    tr.append(h);
  }
  th.append(tr); t.append(th);
  const val = (a, c) => c.v ? c.v(a) : (c.k === "tercio" ? {superior: 3, medio: 2, inferior: 1}[a.tercio] : c.k === "alertas" ? a.alertas.length : a[c.k]);
  const filas = [...A].sort((a, b) => {
    const c = COLS.find(c => c.k === orden.k); let x = val(a, c), y = val(b, c);
    if (x == null) return 1; if (y == null) return -1;
    const r = typeof x === "string" ? x.localeCompare(y, "es") : x - y;
    return orden.asc ? r : -r;
  });
  const tb = el("tbody");
  for (const a of filas) {
    const r = el("tr", {class: seleccion === a.especie ? "sel" : "", tabindex: 0});
    for (const c of COLS) {
      const td = el("td", {class: c.izq ? "izq" : ""});
      if (c.chip) {
        const ch = el("span", {class: "chip"}); const d = el("span", {class: "dot"}); d.style.background = `var(${TERCIO[a.tercio]})`;
        ch.append(d, document.createTextNode(a.tercio)); td.append(ch);
      } else if (c.delta) {
        const v = c.v(a);
        if (v == null) td.textContent = "—";
        else { const n = Math.round(v * 100); td.textContent = n === 0 ? "=" : (n > 0 ? "▲ " : "▼ ") + Math.abs(n); td.className = n > 0 ? "up" : n < 0 ? "down" : ""; }
      } else if (c.k === "alertas") {
        for (const x of a.alertas) td.append(el("div", {class: "alerta"}, x));
        if (!a.alertas.length) td.textContent = "—";
      } else {
        td.textContent = c.f(a);
        if (c.bar) { const b = el("span", {class: "bar"}); const i = el("i"); i.style.width = (c.bar(a) * 100) + "%"; b.append(i); td.append(b); }
      }
      if (c.k === "corto") td.title = a.nombre;
      r.append(td);
    }
    r.addEventListener("click", () => elegir(a.especie));
    r.addEventListener("keydown", ev => { if (ev.key === "Enter") elegir(a.especie); });
    tb.append(r);
  }
  t.append(tb);
}

// ---------------------------------------------------------------- detalle de una acción
function lineaSimple(caja, puntos, opciones) {
  // puntos: [{x: índice, y, etiqueta}]; una sola serie, un solo eje
  const W = Math.max(caja.clientWidth, 300), H = opciones.alto || 170, m = {l: 40, r: 12, t: 10, b: 24};
  const ys = puntos.map(p => p.y).filter(v => v != null);
  if (!ys.length) return;
  let y0 = opciones.min ?? Math.min(...ys), y1 = opciones.max ?? Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const n = puntos.length;
  const X = i => m.l + (n <= 1 ? 0 : i / (n - 1)) * (W - m.l - m.r), Y = v => H - m.b - (v - y0) / (y1 - y0) * (H - m.t - m.b);
  const s = sv("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opciones.titulo});
  for (const b of opciones.bandas || []) s.append(sv("rect", {x: m.l, width: W - m.l - m.r, y: Y(b[1]), height: Y(b[0]) - Y(b[1]), fill: "var(--chip)"}));
  for (const t of opciones.ticks) {
    s.append(sv("line", {x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), stroke: "var(--grid)"}));
    s.append(sv("text", {x: m.l - 6, y: Y(t) + 4, "text-anchor": "end"}, opciones.fy(t)));
  }
  const paso = Math.max(1, Math.ceil(n / 6));
  puntos.forEach((p, i) => { if (i % paso === 0 || i === n - 1) s.append(sv("text", {x: X(i), y: H - 6, "text-anchor": i === n - 1 ? "end" : "middle"}, p.etiqueta)); });
  let d = "", abierto = false;
  puntos.forEach((p, i) => { if (p.y == null) { abierto = false; return; } d += (abierto ? "L" : "M") + X(i).toFixed(1) + "," + Y(p.y).toFixed(1); abierto = true; });
  s.append(sv("path", {d, fill: "none", stroke: "var(--s1)", "stroke-width": 2, "stroke-linejoin": "round"}));
  const ult = [...puntos].reverse().find(p => p.y != null);
  if (ult) { const i = puntos.lastIndexOf(ult); s.append(sv("circle", {cx: X(i), cy: Y(ult.y), r: 4, fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": 2})); }
  const cruz = sv("line", {y1: m.t, y2: H - m.b, stroke: "var(--axis)", visibility: "hidden"});
  const marca = sv("circle", {r: 4, fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden"});
  s.append(cruz, marca);
  const zona = sv("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"});
  zona.addEventListener("pointermove", ev => {
    const r = s.getBoundingClientRect(); const i = Math.round((ev.clientX - r.left - m.l) / (W - m.l - m.r) * (n - 1));
    const p = puntos[Math.max(0, Math.min(n - 1, i))]; const k = puntos.indexOf(p);
    cruz.setAttribute("x1", X(k)); cruz.setAttribute("x2", X(k)); cruz.setAttribute("visibility", "visible");
    if (p.y != null) { marca.setAttribute("cx", X(k)); marca.setAttribute("cy", Y(p.y)); marca.setAttribute("visibility", "visible"); }
    mostrarTip(ev, p.titulo || p.etiqueta, p.filas || [[opciones.titulo, p.y == null ? "—" : opciones.fy(p.y)]]);
  });
  zona.addEventListener("pointerleave", () => { cruz.setAttribute("visibility", "hidden"); marca.setAttribute("visibility", "hidden"); ocultarTip(); });
  s.append(zona);
  caja.append(s);
}

function detalle() {
  const c = $("#detalle"); c.replaceChildren();
  const a = A.find(x => x.especie === seleccion);
  if (!a) { c.append(el("div", {class: "detalle-vacio"}, "Elija una acción en la matriz o en la tabla.")); return; }
  c.append(el("h2", {}, a.corto), el("p", {class: "desc"}, a.nombre));
  const g = el("div", {class: "det-grid"});
  const dato = (l, v) => { const d = el("div"); d.append(el("span", {}, l), el("b", {}, v)); g.append(d); };
  dato("Puntaje", `${p100(a.puntaje)} (posición ${a.posicion} de ${a.n})`);
  dato("Tercio", TNOM[a.tercio]);
  dato("Percentil del puntaje: hoy · hace 1 y 3 meses", `${p100(a.pp)} · ${p100(a.pp1)} · ${p100(a.pp3)}`);
  dato("Último negocio", `${fmes(a.ultimo)} a ${num(a.precio, 0)}`);
  dato("Retorno del último mes", pct(a.r1));
  dato("Días con negocio (60)", pct(a.frac60, 0));
  dato("Valor patrimonial", a.corte_vpa ? `corte ${fmes(a.corte_vpa)}` : "sin dato");
  c.append(g);
  for (const x of a.alertas) c.append(el("div", {class: "alerta"}, x));
  const h = a.historia;
  c.append(el("h2", {style: "font-size:14px;margin-top:12px"}, "Percentil del puntaje, últimos meses"));
  const c1 = el("div"); c.append(c1);
  lineaSimple(c1, h.map(p => ({y: p[1], etiqueta: fmesc(p[0]), titulo: fmes(p[0]),
    filas: [["Percentil del puntaje", p100(p[1])], ["Tercio", p[3] || "fuera del universo"]]})),
    {titulo: "Percentil del puntaje", min: 0, max: 1, ticks: [0, 1 / 3, 2 / 3, 1], fy: t => Math.round(t * 100), bandas: [[2 / 3, 1]], alto: 160});
  c.append(el("p", {class: "desc", style: "margin:4px 0 10px;font-size:12px"}, "La franja sombreada es el tercio superior. Los huecos son meses en que la acción estuvo fuera del universo."));
  c.append(el("h2", {style: "font-size:14px"}, "Retorno total acumulado (base 100)"));
  let i = 100; const pts = h.map(p => { if (p[2] != null) i *= 1 + p[2]; return {y: i, etiqueta: fmesc(p[0]), titulo: fmes(p[0]), filas: [["Índice", num(i, 1)], ["Retorno del mes", pct(p[2])]]}; });
  const ys = pts.map(p => p.y), lo = Math.min(...ys), hi = Math.max(...ys);
  const c2 = el("div"); c.append(c2);
  lineaSimple(c2, pts, {titulo: "Índice", min: lo * .95, max: hi * 1.05, ticks: [lo, (lo + hi) / 2, hi].map(v => Math.round(v)), fy: t => Math.round(t), alto: 150});
}

function elegir(e) { seleccion = e; matriz(); tabla(); detalle(); }

// ---------------------------------------------------------------- historia del tercio superior
function historia() {
  const caja = $("#historia"); caja.replaceChildren();
  const S = D.serie; if (!S.length) { caja.textContent = "Sin historia."; return; }
  const W = Math.max(caja.clientWidth, 360), H = 320, m = {l: 52, r: 130, t: 12, b: 26};
  const vals = S.flatMap(r => [r[1], r[2], r[3]]).filter(v => v > 0);
  const l0 = Math.log(Math.min(...vals, 100) * .9), l1 = Math.log(Math.max(...vals) * 1.1);
  const X = i => m.l + i / (S.length - 1) * (W - m.l - m.r), Y = v => H - m.b - (Math.log(v) - l0) / (l1 - l0) * (H - m.t - m.b);
  const s = sv("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Índice acumulado del tercio superior y del universo, escala logarítmica"});
  for (const t of [50, 100, 200, 400, 800, 1600, 3200]) {
    if (Math.log(t) < l0 || Math.log(t) > l1) continue;
    s.append(sv("line", {x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), stroke: t === 100 ? "var(--axis)" : "var(--grid)"}));
    s.append(sv("text", {x: m.l - 6, y: Y(t) + 4, "text-anchor": "end"}, String(t)));
  }
  let ultimoAnio = null;
  S.forEach((r, i) => { const y = r[0].slice(0, 4); if (y !== ultimoAnio && +y % 2 === 0 && r[0].slice(5, 7) === "01") { s.append(sv("text", {x: X(i), y: H - 6, "text-anchor": "middle"}, y)); } ultimoAnio = y; });
  const series = [[1, "--s1", "Tercio superior, bruto", "Superior bruto"], [2, "--s2", "Tercio superior, neto de costos", "Superior neto"], [3, "--s3", "Universo (pesos iguales)", "Universo"]];
  const fin = [];
  for (const [k, color, nombre, corto] of series) {
    const d = S.map((r, i) => (i ? "L" : "M") + X(i).toFixed(1) + "," + Y(r[k]).toFixed(1)).join("");
    s.append(sv("path", {d, fill: "none", stroke: `var(${color})`, "stroke-width": 2, "stroke-linejoin": "round"}));
    fin.push({y: Y(S[S.length - 1][k]), corto, v: S[S.length - 1][k]});
  }
  fin.sort((a, b) => a.y - b.y);
  for (let i = 1; i < fin.length; i++) if (fin[i].y - fin[i - 1].y < 14) fin[i].y = fin[i - 1].y + 14;
  for (const f of fin) s.append(sv("text", {x: W - m.r + 8, y: f.y + 4, class: "lbl"}, `${f.corto} ${Math.round(f.v).toLocaleString("es-CO")}`));
  const cruz = sv("line", {y1: m.t, y2: H - m.b, stroke: "var(--axis)", visibility: "hidden"}); s.append(cruz);
  const marcas = series.map(([, color]) => { const c = sv("circle", {r: 4, fill: `var(${color})`, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden"}); s.append(c); return c; });
  const zona = sv("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"});
  zona.addEventListener("pointermove", ev => {
    const r = s.getBoundingClientRect(); const i = Math.max(0, Math.min(S.length - 1, Math.round((ev.clientX - r.left - m.l) / (W - m.l - m.r) * (S.length - 1))));
    cruz.setAttribute("x1", X(i)); cruz.setAttribute("x2", X(i)); cruz.setAttribute("visibility", "visible");
    series.forEach(([k], j) => { marcas[j].setAttribute("cx", X(i)); marcas[j].setAttribute("cy", Y(S[i][k])); marcas[j].setAttribute("visibility", "visible"); });
    mostrarTip(ev, `Hasta el mes siguiente a ${fmes(S[i][0])}`, [...series.map(([k, , n]) => [n, num(S[i][k], 0)]), ["Acciones en el universo", String(S[i][4])]]);
  });
  zona.addEventListener("pointerleave", () => { cruz.setAttribute("visibility", "hidden"); marcas.forEach(c => c.setAttribute("visibility", "hidden")); ocultarTip(); });
  s.append(zona); caja.append(s);
  const ley = $("#ley-hist"); ley.replaceChildren();
  for (const [, color, nombre] of series) { const sp = el("span"); const i = el("i"); i.style.background = `var(${color})`; sp.append(i, document.createTextNode(nombre)); ley.append(sp); }
  const P = D.parametros;
  $("#desc-hist").textContent = `Si desde ${fmes(S[0][0])} se hubieran tenido, mes a mes y en partes iguales, las acciones del tercio superior del puntaje (escala logarítmica, base 100). ` +
    `El neto descuenta ${pct(P.costo_ida, 1)} por cada compra y cada venta de lo que entra y sale del tercio. ` +
    (comb ? `Exceso mensual promedio sobre el universo: ${pct(comb.exceso, 2)} bruto y ${pct(comb.exceso_neto, 2)} neto (t de Newey-West ${num(comb.exceso_neto_t, 1)}; por debajo de 2 no se distingue del azar con confianza). ` : "") +
    `No incluye impuestos, el costo de no poder comprar la cantidad deseada en acciones poco líquidas ni el sesgo de haber elegido estos factores mirando datos que se solapan con esta historia.`;
}

// ---------------------------------------------------------------- evidencia
function evidencia() {
  const P = D.parametros;
  $("#desc-evid").textContent = `IC: correlación de Spearman entre el factor y el retorno del mes siguiente, en promedio mensual desde ${fmes(P.inicio)}; t de Newey-West; valor p ajustado por Holm porque se prueban varios factores a la vez. ` +
    `"Validado" exige p ajustado menor a 5 % y un IC positivo antes de 2018 (descubrimiento) y después (fuera de muestra). El puntaje combinado se mide con los mismos datos que eligieron sus factores: es informativo, no una validación independiente.`;
  const t = $("#evidencia"); t.replaceChildren();
  const cab = ["Factor", "Hipótesis", "Veredicto", "Meses", "IC medio", "t", "p (Holm)", "IC antes de 2018", "IC desde 2018", "Exceso neto del tercio superior", "Rotación"];
  const th = el("thead"); const tr = el("tr"); cab.forEach((c, i) => tr.append(el("th", {class: i < 3 ? "izq" : "", scope: "col", style: "cursor:default"}, c))); th.append(tr); t.append(th);
  const tb = el("tbody");
  for (const f of D.factores) {
    const r = el("tr", {style: "cursor:default"});
    r.append(el("td", {class: "izq"}, f.nombre));
    const d = el("td", {class: "izq", style: "white-space:normal;max-width:36ch;color:var(--ink2)"}, f.descripcion); r.append(d);
    const v = el("td", {class: "izq"}); v.append(el("span", {class: "veredicto " + f.veredicto}, f.veredicto.replace("_", " "))); r.append(v);
    for (const x of [String(f.meses ?? "—"), num(f.ic, 3), num(f.ic_t, 1), f.p_holm == null ? "—" : num(f.p_holm, 3), num(f.ic_desc, 3), num(f.ic_fuera, 3), pct(f.exceso_neto, 2), pct(f.rotacion, 0)]) r.append(el("td", {}, x));
    tb.append(r);
  }
  t.append(tb);
}

// ---------------------------------------------------------------- fuera del universo
function fuera() {
  const t = $("#fuera"); t.replaceChildren();
  const th = el("thead"); const tr = el("tr");
  ["Acción", "Motivo", "Último negocio", "Días con negocio (60)", "Negociado/día"].forEach((c, i) => tr.append(el("th", {class: i < 2 ? "izq" : "", scope: "col", style: "cursor:default"}, c)));
  th.append(tr); t.append(th);
  const tb = el("tbody");
  for (const f of [...D.fuera].sort((a, b) => a.corto.localeCompare(b.corto, "es"))) {
    const r = el("tr", {style: "cursor:default"});
    const n = el("td", {class: "izq"}, f.corto); n.title = f.nombre; r.append(n);
    r.append(el("td", {class: "izq"}, f.motivo), el("td", {}, fmes(f.ultimo)), el("td", {}, pct(f.frac60, 0)), el("td", {}, mm(f.monto60)));
    tb.append(r);
  }
  t.append(tb);
}

$("#pie").textContent = `Fuentes: precios promedio y eventos de la Superintendencia Financiera, dividendos de los avisos de la SFC y de historiales públicos verificados, anexos NIIF del SIMEV. ` +
  `Retornos totales con dividendos y ajustes por splits y escisiones; los saltos de precio sin explicar cuentan como cero. ` +
  `Los factores usan solo lo que se sabía al cierre de cada mes. P/VL y P/U son de los estados separados y del último cierre anual conocido. Generado por invest tablero.`;

function todo() { matriz(); tabla(); detalle(); historia(); evidencia(); fuera(); }
todo();
let espera; addEventListener("resize", () => { clearTimeout(espera); espera = setTimeout(() => { matriz(); detalle(); historia(); }, 150); });
</script>
</body>
</html>
"""
