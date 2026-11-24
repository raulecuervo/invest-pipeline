"use strict";
// Aplicativo local Invest: páginas sobre la API de invest.app.servidor. Sin librerías externas.

// ------------------------------------------------------------------ utilidades
const $ = (s) => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const estado = { variables: {}, activos: [], info: null };

function el(tag, attrs = {}, ...hijos) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const h of hijos.flat()) if (h != null && h !== false) e.append(h instanceof Node ? h : String(h));
  return e;
}
function sv(tag, attrs = {}, texto) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  if (texto != null) e.textContent = texto;
  return e;
}
async function api(ruta, opciones) {
  const r = await fetch(ruta, opciones);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
const postear = (ruta, cuerpo) => api(ruta, { method: "POST", headers: { "Content-Type": "application/json", "X-Invest": "1" }, body: JSON.stringify(cuerpo || {}) });

const NF = (d) => new Intl.NumberFormat("es-CO", { minimumFractionDigits: d, maximumFractionDigits: d });
const num = (x, d = 1) => x == null ? "—" : NF(d).format(x);
const pct = (x, d = 1) => x == null ? "—" : NF(d).format(x * 100) + " %";
const pp = (x, d = 2) => x == null ? "—" : NF(d).format(x) + " %";
const veces = (x, d = 2) => x == null ? "—" : NF(d).format(x) + "x";
function pesos(x) {
  if (x == null) return "—";
  const a = Math.abs(x);
  if (a >= 1e12) return "$ " + NF(1).format(x / 1e12) + " billones";
  if (a >= 1e9) return "$ " + NF(1).format(x / 1e9) + " mil M";
  if (a >= 1e6) return "$ " + NF(1).format(x / 1e6) + " M";
  return "$ " + NF(0).format(x);
}
const MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
const fecha = (s) => { if (!s) return "—"; const [y, m, d] = s.slice(0, 10).split("-"); return `${+d} ${MESES[+m - 1]} ${y}`; };
const fechaCorta = (s) => { const [y, m] = s.slice(0, 7).split("-"); return `${MESES[+m - 1]} ${y.slice(2)}`; };
function segunFormato(v, formato) {
  switch (formato) {
    case "porcentaje": return pct(v);
    case "porcentaje_puntos": return pp(v);
    case "veces": return veces(v);
    case "pesos": return pesos(v);
    case "puntaje": return num(v, 1);
    case "si_no": return v == null ? "—" : (v ? "sí" : "no");
    default: return typeof v === "number" ? num(v, 2) : (v ?? "—");
  }
}
function banda(p) {
  if (p == null) return "b-nd";
  if (p >= 8) return "b-fuerte"; if (p >= 6) return "b-bueno"; if (p >= 4) return "b-neutral"; return "b-debil";
}
const COLOR_BANDA = { "b-fuerte": "#3DD68C", "b-bueno": "#6E9BFF", "b-neutral": "#F5B54A", "b-debil": "#FF7A6B", "b-nd": "#5B6B8C" };
const notaPuntaje = (p) => el("span", { class: "nota-puntaje " + banda(p) }, p == null ? "N/D" : num(p, 1));
const CUADRANTE = { ambos_altos: "Ambos altos", fundamental_alto: "Fundamental alto", tecnico_alto: "Técnico alto", ambos_bajos: "Ambos bajos" };
const nombreActivo = (r) => r.nombre_corto || r.emisor_corto || r.nemotecnico || r.especie;

// ------------------------------------------------------------------ tooltip y explicación de variables
const tip = $("#tip");
function mostrarTip(ev, titulo, filas) {
  tip.replaceChildren(el("b", {}, titulo), ...filas.map(([a, b]) => el("div", { class: "r" }, el("span", {}, a), el("span", {}, b))));
  tip.style.display = "block";
  tip.style.left = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8) + "px";
  tip.style.top = Math.min(ev.clientY + 14, innerHeight - tip.offsetHeight - 8) + "px";
}
const ocultarTip = () => { tip.style.display = "none"; };

const pop = $("#popover");
function info(variableId, valorTexto) {
  return el("button", {
    type: "button", class: "info", "aria-label": "Qué es " + (estado.variables[variableId]?.nombre || variableId),
    onclick: (ev) => { ev.stopPropagation(); abrirVariable(variableId, ev.currentTarget, valorTexto); },
  }, iconoInfo());
}
function iconoInfo() {
  const s = sv("svg", { width: 14, height: 14, viewBox: "0 0 16 16", "aria-hidden": "true" });
  s.append(sv("circle", { cx: 8, cy: 8, r: 6.75, fill: "none", stroke: "currentColor", "stroke-width": 1.5 }),
    sv("path", { d: "M8 7.2v4", stroke: "currentColor", "stroke-width": 1.5, "stroke-linecap": "round" }),
    sv("circle", { cx: 8, cy: 4.8, r: 0.95, fill: "currentColor" }));
  return s;
}
function abrirVariable(id, ancla, valorTexto) {
  const v = estado.variables[id];
  pop.replaceChildren();
  if (!v) { pop.append(el("p", {}, "Sin explicación todavía para " + id + ".")); }
  else {
    pop.append(
      el("button", { type: "button", class: "info cerrar", "aria-label": "Cerrar", onclick: cerrarPop }, "✕"),
      el("div", { class: "t" }, "Sobre esta variable · " + v.nombre + (v.revisado ? "" : " (borrador)")),
      el("p", {}, el("strong", {}, "Qué es: "), v.que_es),
      el("p", {}, el("strong", {}, "Qué hace: "), v.que_hace),
      el("p", {}, el("strong", {}, "Para qué sirve: "), v.para_que_sirve),
      el("p", {}, el("strong", {}, "Cómo leerla: "), v.como_leer),
      valorTexto ? el("p", { class: "formula" }, "Aquí: " + valorTexto) : null,
      el("p", { class: "formula" }, "Fórmula: " + v.formula),
      el("a", { href: "#/aprender/" + v.id, onclick: cerrarPop, style: { color: "var(--acento)", fontWeight: 600, fontSize: "13px" } }, "Saber más →"));
  }
  pop.hidden = false;
  const r = ancla.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, innerWidth - pop.offsetWidth - 8)) + "px";
  pop.style.top = Math.max(8, Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 8)) + "px";
}
function cerrarPop() { pop.hidden = true; }
document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target)) cerrarPop(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") cerrarPop(); });

// ------------------------------------------------------------------ gráficos (SVG propio)
function lineas(caja, series, op = {}) {
  // series: [{nombre, color, puntos: [{x: 'AAAA-MM-DD', y}], discontinua}] sobre un solo eje
  caja.replaceChildren();
  const W = Math.max(caja.clientWidth || 600, 320), H = op.alto || 240, m = { l: 56, r: op.derecha || 16, t: 10, b: 24 };
  const todas = series.flatMap((s) => s.puntos.filter((p) => p.y != null && isFinite(p.y)));
  if (!todas.length) { caja.append(el("div", { class: "vacio" }, op.vacio || "Sin datos.")); return; }
  const xs = [...new Set(todas.map((p) => p.x))].sort();
  const ix = new Map(xs.map((x, i) => [x, i]));
  let y0 = op.min ?? Math.min(...todas.map((p) => p.y)), y1 = op.max ?? Math.max(...todas.map((p) => p.y));
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.06; if (op.min == null) y0 -= pad; if (op.max == null) y1 += pad;
  const X = (x) => m.l + (xs.length < 2 ? 0 : ix.get(x) / (xs.length - 1)) * (W - m.l - m.r);
  const Y = (y) => H - m.b - (y - y0) / (y1 - y0) * (H - m.t - m.b);
  const s = sv("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": op.titulo || "Gráfico" });
  const fy = op.fy || ((v) => num(v, 1));
  for (let i = 0; i <= 4; i++) {
    const v = y0 + (y1 - y0) * i / 4;
    s.append(sv("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: "#1A2540" }), sv("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end" }, fy(v)));
  }
  const paso = Math.max(1, Math.ceil(xs.length / 7));
  xs.forEach((x, i) => { if (i % paso === 0) s.append(sv("text", { x: X(x), y: H - 6, "text-anchor": "middle" }, op.fx ? op.fx(x) : fechaCorta(x))); });
  for (const se of series) {
    let d = "", abierto = false;
    for (const p of se.puntos) {
      if (p.y == null || !isFinite(p.y)) { abierto = false; continue; }
      d += (abierto ? "L" : "M") + X(p.x).toFixed(1) + "," + Y(p.y).toFixed(1); abierto = true;
    }
    s.append(sv("path", { d, fill: "none", stroke: se.color, "stroke-width": se.grosor || 2, "stroke-dasharray": se.discontinua ? "6 5" : null, "stroke-linejoin": "round" }));
  }
  const cruz = sv("line", { y1: m.t, y2: H - m.b, stroke: "#5B6B8C", visibility: "hidden" });
  s.append(cruz);
  const zona = sv("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" });
  zona.addEventListener("pointermove", (ev) => {
    const r = s.getBoundingClientRect();
    const i = Math.max(0, Math.min(xs.length - 1, Math.round((ev.clientX - r.left - m.l) / (W - m.l - m.r) * (xs.length - 1))));
    const x = xs[i];
    cruz.setAttribute("x1", X(x)); cruz.setAttribute("x2", X(x)); cruz.setAttribute("visibility", "visible");
    mostrarTip(ev, fecha(x), series.map((se) => { const p = se.puntos.find((q) => q.x === x); return [se.nombre, p && p.y != null ? fy(p.y) : "—"]; }));
  });
  zona.addEventListener("pointerleave", () => { cruz.setAttribute("visibility", "hidden"); ocultarTip(); });
  s.append(zona);
  caja.append(s);
}

function leyenda(items) {
  return el("div", { class: "leyenda" }, items.map(([color, texto, tipo]) => el("span", {}, el("i", { class: tipo === "linea" ? "linea" : "", style: { background: color } }), texto)));
}

// Mapa de árbol "squarified" (Bruls, Huizing y van Wijk)
function cuadricular(items, x, y, w, h) {
  const total = items.reduce((a, b) => a + b.valor, 0);
  const salida = [];
  let resto = items.map((it) => ({ ...it, area: it.valor / total * w * h }));
  while (resto.length) {
    const corto = Math.min(w, h);
    let fila = [], peor = Infinity;
    for (const it of resto) {
      const prueba = [...fila, it];
      const s = prueba.reduce((a, b) => a + b.area, 0);
      const mx = Math.max(...prueba.map((p) => p.area)), mn = Math.min(...prueba.map((p) => p.area));
      const p = Math.max(corto * corto * mx / (s * s), s * s / (corto * corto * mn));
      if (p > peor) break;
      fila = prueba; peor = p;
    }
    const s = fila.reduce((a, b) => a + b.area, 0);
    if (w >= h) {
      const ancho = s / h; let yy = y;
      for (const it of fila) { const alto = it.area / ancho; salida.push({ ...it, x, y: yy, w: ancho, h: alto }); yy += alto; }
      x += ancho; w -= ancho;
    } else {
      const alto = s / w; let xx = x;
      for (const it of fila) { const ancho = it.area / alto; salida.push({ ...it, x: xx, y, w: ancho, h: alto }); xx += ancho; }
      y += alto; h -= alto;
    }
    resto = resto.slice(fila.length);
  }
  return salida;
}
function mapaArbol(caja, items, op) {
  caja.replaceChildren();
  const W = Math.max(caja.clientWidth || 600, 320), H = op.alto || 380;
  const validos = items.filter((i) => i.valor > 0).sort((a, b) => b.valor - a.valor);
  if (!validos.length) { caja.append(el("div", { class: "vacio" }, "Sin datos.")); return; }
  const s = sv("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": op.titulo });
  for (const r of cuadricular(validos, 0, 0, W, H)) {
    const g = sv("g", { style: "cursor:pointer" });
    g.append(sv("rect", { x: r.x + 1.5, y: r.y + 1.5, width: Math.max(r.w - 3, 0), height: Math.max(r.h - 3, 0), rx: 6, fill: op.color(r) }));
    // Solo se rotula lo que cabe; el resto se lee en el tooltip
    if (r.w > r.etiqueta.length * 7.4 + 16 && r.h > 34) {
      g.append(sv("text", { x: r.x + 10, y: r.y + 22, class: "et" }, r.etiqueta));
      if (r.h > 52 && r.w > (r.sub || "").length * 6.4 + 16) g.append(sv("text", { x: r.x + 10, y: r.y + 40, class: "et2" }, r.sub));
    }
    g.addEventListener("pointermove", (ev) => mostrarTip(ev, r.etiqueta, r.filas));
    g.addEventListener("pointerleave", ocultarTip);
    if (r.href) g.addEventListener("click", () => { location.hash = r.href; });
    s.append(g);
  }
  caja.append(s);
}
// Divergente: rojo – gris – verde (retornos)
function divergente(v, lim) {
  if (v == null) return "#2A3448";
  const t = Math.max(-1, Math.min(1, v / lim));
  const mezcla = (a, b, k) => "#" + [0, 2, 4].map((i) => Math.round(parseInt(a.slice(1 + i, 3 + i), 16) * (1 - k) + parseInt(b.slice(1 + i, 3 + i), 16) * k).toString(16).padStart(2, "0")).join("");
  return t < 0 ? mezcla("#2A3448", "#8A2E26", -t) : mezcla("#2A3448", "#1F7A4F", t);
}

function dispersion(caja, puntos, op) {
  // puntos: [{x, y, etiqueta, color, href, filas}] en escala 1-10
  caja.replaceChildren();
  const W = Math.max(caja.clientWidth || 600, 320), H = op.alto || 460, m = { l: 48, r: 16, t: 12, b: 40 };
  const X = (v) => m.l + (v - 1) / 9 * (W - m.l - m.r), Y = (v) => H - m.b - (v - 1) / 9 * (H - m.t - m.b);
  const s = sv("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": op.titulo });
  for (const t of [1, 3, 5.5, 8, 10]) {
    const medio = t === 5.5;
    s.append(sv("line", { x1: X(t), x2: X(t), y1: Y(1), y2: Y(10), stroke: medio ? "#5B6B8C" : "#1A2540", "stroke-dasharray": medio ? "4 3" : null }));
    s.append(sv("line", { y1: Y(t), y2: Y(t), x1: X(1), x2: X(10), stroke: medio ? "#5B6B8C" : "#1A2540", "stroke-dasharray": medio ? "4 3" : null }));
    s.append(sv("text", { x: X(t), y: H - m.b + 16, "text-anchor": "middle" }, num(t, t % 1 ? 1 : 0)));
    s.append(sv("text", { x: m.l - 8, y: Y(t) + 4, "text-anchor": "end" }, num(t, t % 1 ? 1 : 0)));
  }
  s.append(sv("text", { x: (X(1) + X(10)) / 2, y: H - 6, "text-anchor": "middle", class: "et2" }, op.ejeX));
  const yM = (Y(1) + Y(10)) / 2;
  s.append(sv("text", { x: 12, y: yM, "text-anchor": "middle", class: "et2", transform: `rotate(-90 12 ${yM})` }, op.ejeY));
  for (const [qx, qy, a, t] of op.cuadrantes || []) s.append(sv("text", { x: X(qx), y: Y(qy), "text-anchor": a }, t));
  const ocupadas = [];
  for (const p of [...puntos].sort((a, b) => Y(a.y) - Y(b.y))) {
    const cx = X(p.x), cy = Y(p.y), ancho = p.etiqueta.length * 6.6;
    const izq = cx + 10 + ancho > W - m.r;
    let ly = cy + 4;
    const x0 = izq ? cx - 10 - ancho : cx + 10, x1 = x0 + ancho;
    for (let i = 0; i < 10 && ocupadas.some((o) => o.x0 < x1 && o.x1 > x0 && Math.abs(o.y - ly) < 12); i++) ly += 12;
    ocupadas.push({ x0, x1, y: ly });
    const g = sv("g", { style: "cursor:pointer", tabindex: 0 });
    g.append(sv("circle", { cx, cy, r: 14, fill: "transparent" }), sv("circle", { cx, cy, r: 6, fill: p.color, stroke: "#0B1220", "stroke-width": 2 }),
      sv("text", { x: izq ? cx - 10 : cx + 10, y: ly, "text-anchor": izq ? "end" : "start", class: "et2" }, p.etiqueta));
    g.addEventListener("pointermove", (ev) => mostrarTip(ev, p.etiqueta, p.filas));
    g.addEventListener("pointerleave", ocultarTip);
    g.addEventListener("click", () => { location.hash = p.href; });
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter") location.hash = p.href; });
    s.append(g);
  }
  caja.append(s);
}

// Tabla ordenable: columnas [{k, t, f, izq, v, info}]
function tabla(columnas, filas, op = {}) {
  let orden = { k: op.orden || null, asc: op.asc ?? false };
  const t = el("table");
  const pintar = () => {
    t.replaceChildren();
    const tr = el("tr");
    for (const c of columnas) {
      const th = el("th", { class: c.izq ? "izq" : "", scope: "col", onclick: () => { orden = { k: c.k, asc: orden.k === c.k ? !orden.asc : !!c.izq }; pintar(); } },
        c.t + (orden.k === c.k ? (orden.asc ? " ▲" : " ▼") : ""));
      if (c.info) th.append(info(c.info));
      tr.append(th);
    }
    t.append(el("thead", {}, tr));
    const val = (r, c) => c.v ? c.v(r) : r[c.k];
    const datos = orden.k ? [...filas].sort((a, b) => {
      const c = columnas.find((x) => x.k === orden.k); const x = val(a, c), y = val(b, c);
      if (x == null) return 1; if (y == null) return -1;
      const r = typeof x === "string" ? x.localeCompare(y, "es") : x - y; return orden.asc ? r : -r;
    }) : filas;
    const tb = el("tbody");
    for (const r of datos) {
      const fila = el("tr", { class: op.clic ? "clic" : "", onclick: op.clic ? () => op.clic(r) : null });
      for (const c of columnas) {
        const contenido = c.f ? c.f(r) : (r[c.k] ?? "—");
        fila.append(el("td", { class: c.izq ? "izq" : "" }, contenido));
      }
      tb.append(fila);
    }
    t.append(tb);
  };
  pintar();
  return el("div", { class: "tabla-caja" }, t);
}
function csv(nombre, columnas, filas) {
  const esc = (v) => { const s = v == null ? "" : String(v); return /[",;\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const texto = [columnas.map((c) => c.t).join(";"), ...filas.map((r) => columnas.map((c) => esc(c.v ? c.v(r) : r[c.k])).join(";"))].join("\n");
  const a = el("a", { href: URL.createObjectURL(new Blob(["﻿" + texto], { type: "text/csv" })), download: nombre });
  document.body.append(a); a.click(); a.remove();
}

const panel = (titulo, ...hijos) => el("section", { class: "panel" }, titulo ? el("h2", {}, titulo) : null, ...hijos);
const kpi = (l, v, d, clase, varId) => el("div", {}, el("div", { class: "l" }, l, varId ? info(varId) : null), el("div", { class: "v" }, v), d ? el("div", { class: "d " + (clase || "") }, d) : null);
const cargando = () => el("div", { class: "vacio" }, "Cargando…");

// ------------------------------------------------------------------ páginas
const paginas = {};

paginas.mercado = async (v) => {
  const d = await api("/api/mercado");
  v.append(el("div", { class: "encabezado" }, el("div", {}, el("h1", {}, "Mercado y país"), el("div", { class: "sub" }, "Colombia · datos al " + fecha(estado.info?.ultimo_precio)))));
  const k = Object.fromEntries(d.kpis.map((r) => [r.serie_id, r]));
  const delta = (r, fmtD) => r && r.anterior != null ? fmtD(r.valor - r.anterior) : null;
  const claseD = (r) => r && r.anterior != null ? (r.valor >= r.anterior ? "sube" : "baja") : "";
  const ind = el("section", { class: "indicadores", "aria-label": "Indicadores" });
  const trm = k.trm_usd_cop, tpm = k.banrep_59, inf = k.inflacion_co_12m, col = k.msci_colcap, t10 = k.fred_dgs10, vix = k.fred_vixcls;
  ind.append(
    kpi("TRM", trm ? "$ " + num(trm.valor, 2) : "—", trm ? "vigente el " + fecha(trm.fecha) : null, "", "trm_usd_cop"),
    kpi("Tasa BanRep", tpm ? pp(tpm.valor) : "—", tpm ? "al " + fecha(tpm.fecha) : null, "", "banrep_59"),
    kpi("Inflación anual", inf ? pp(inf.valor) : "—", inf ? fechaCorta(inf.fecha) : null, "", "inflacion_co_12m"),
    kpi("MSCI COLCAP", col ? num(col.valor, 2) : "—", col && delta(col, (x) => (x >= 0 ? "+" : "") + num(x / col.anterior * 100, 2) + " %"), claseD(col)),
    kpi("Tesoro EE. UU. 10 años", t10 ? pp(t10.valor) : "—", vix ? "VIX " + num(vix.valor, 1) : null));
  v.append(ind);

  const cajaMapa = el("div");
  let colorPor = "retorno_1m";
  const botonesMapa = el("div", { class: "grupo", role: "group", "aria-label": "Color del mapa" });
  const opciones = [["retorno_1m", "Retorno 1 mes"], ["retorno_12m", "Retorno 12 meses"], ["puntaje_global", "Puntaje"]];
  const liquidas = el("input", { type: "checkbox", id: "solo-liquidas", checked: true });
  const pintarMapa = () => {
    const filas = d.mapa.filter((r) => !liquidas.checked || r.en_universo);
    mapaArbol(cajaMapa, filas.map((r) => ({
      valor: r.capitalizacion, etiqueta: r.nemotecnico || r.emisor_corto || r.especie,
      sub: colorPor === "puntaje_global" ? "Puntaje " + num(r.puntaje_global, 1) : pct(r[colorPor]),
      href: "#/ficha/" + r.especie, r,
      filas: [["Empresa", r.emisor_corto || "—"], ["Capitalización", pesos(r.capitalizacion)], ["Retorno 1 mes", pct(r.retorno_1m)],
        ["Retorno 12 meses", pct(r.retorno_12m)], ["Puntaje global", num(r.puntaje_global, 1)], ["Caída desde máx. 52 sem.", pct(r.caida_desde_max_52s)]],
    })), {
      alto: 380, titulo: "Mapa del mercado por capitalización",
      color: (it) => colorPor === "puntaje_global" ? (it.r.puntaje_global == null ? "#2A3448" : divergente(it.r.puntaje_global - 5.5, 3)) : divergente(it.r[colorPor], colorPor === "retorno_1m" ? 0.1 : 0.3),
    });
    botonesMapa.replaceChildren(...opciones.map(([k2, t]) => el("button", { type: "button", "aria-pressed": String(colorPor === k2), onclick: () => { colorPor = k2; pintarMapa(); } }, t)));
  };
  liquidas.addEventListener("change", pintarMapa);
  v.append(el("div", { class: "fila" },
    el("section", { class: "panel ancho" },
      el("div", { class: "encabezado" }, el("h2", {}, "Mapa del mercado · tamaño = capitalización"), botonesMapa),
      cajaMapa,
      el("div", { class: "encabezado" }, leyenda([["#8A2E26", "baja"], ["#2A3448", "neutro"], ["#1F7A4F", "sube"]]),
        el("label", { style: { fontSize: "12px", color: "var(--tenue)", display: "flex", gap: "6px", alignItems: "center" } }, liquidas, "Solo acciones líquidas")),
      el("p", { class: "desc" }, "La capitalización de acciones casi sin negocios (p. ej. después de una OPA) puede verse inflada: el filtro las oculta.")),
    panel("Movimientos del " + fecha(d.fecha_movimientos),
      d.movimientos.length ? d.movimientos.map((r) => el("div", { class: "evento" },
        el("a", { href: "#/ficha/" + r.especie, style: { fontWeight: 600, minWidth: "90px" } }, r.nemotecnico || r.nombre_corto),
        el("span", { class: r.retorno_total >= 0 ? "sube" : "baja", style: { minWidth: "64px", textAlign: "right" } }, pct(r.retorno_total, 1)),
        el("span", { class: "nota" }, r.eventos ? "¿Por qué? " + r.eventos : "Sin información relevante publicada esos días"))) : el("div", { class: "vacio" }, "Sin movimientos."))));
  requestAnimationFrame(pintarMapa);

  const cajaIndice = el("div"), cajaMacro = el("div");
  const catalogo = await api("/api/macro");
  const selector = el("select", { "aria-label": "Serie macro", style: { height: "32px", background: "var(--fondo)", border: "1px solid var(--borde-boton)", borderRadius: "6px" } },
    catalogo.map((c) => el("option", { value: c.serie_id }, `${c.corto} (${c.categoria})`)));
  selector.value = catalogo.some((c) => c.serie_id === "trm_usd_cop") ? "trm_usd_cop" : catalogo[0]?.serie_id;
  const pintarMacro = async () => {
    const c = catalogo.find((x) => x.serie_id === selector.value);
    const datos = await api("/api/macro/" + selector.value + "?anios=10");
    const esPct = ["pct", "%"].includes(c?.unidad);
    lineas(cajaMacro, [{ nombre: c?.corto, color: "#6E9BFF", puntos: datos.map((r) => ({ x: r.fecha, y: r.valor })) }],
      { alto: 220, titulo: c?.nombre, fy: (x) => esPct ? pp(x) : num(x, x > 100 ? 0 : 2), vacio: "Esta serie todavía no tiene historia." });
  };
  selector.addEventListener("change", pintarMacro);
  v.append(el("div", { class: "fila" },
    panel("Índice del universo líquido · 12 meses", cajaIndice, el("p", { class: "desc" }, "Retorno total con pesos iguales de las acciones líquidas (base 100 hace un año). Sirve de referencia mientras el MSCI COLCAP acumula historia.")),
    el("section", { class: "panel" }, el("div", { class: "encabezado" }, el("h2", {}, "Macro · 10 años"), selector), cajaMacro)));
  requestAnimationFrame(() => {
    const base = d.indice[0]?.valor;
    lineas(cajaIndice, [{ nombre: "Universo", color: "#6E9BFF", puntos: d.indice.map((r) => ({ x: r.fecha, y: r.valor / base * 100 })) }], { alto: 220, titulo: "Índice del universo", fy: (x) => num(x, 0) });
    pintarMacro();
  });

  const TIPOS = { dividendo_ex: "Fecha ex", dividendo_pago: "Pago de dividendo", banrep_decision: "Banco de la República", dane_ipc: "IPC del DANE" };
  v.append(panel("Próximos 45 días", d.eventos.length ? tabla([
    { k: "fecha", t: "Fecha", izq: true, f: (r) => fecha(r.fecha) + (r.estimada ? " (estimada)" : "") },
    { k: "tipo", t: "Evento", izq: true, f: (r) => TIPOS[r.tipo] || r.tipo },
    { k: "emisor", t: "Emisor", izq: true },
    { k: "descripcion", t: "Detalle", izq: true },
    { k: "valor", t: "Valor", f: (r) => r.tipo.startsWith("dividendo") ? num(r.valor, 2) : "" },
  ], d.eventos) : el("div", { class: "vacio" }, "Nada programado.")));
};

paginas.screener = async (v) => {
  const d = await api("/api/screener");
  let elegido = null, sector = "", soloUniverso = true, minimo = 0;
  v.append(el("div", { class: "encabezado" }, el("div", {}, el("h1", {}, "Screener"),
    el("div", { class: "sub" }, `Corrida del ${fecha(d.fecha)}` + (d.fecha_anterior ? ` · comparada con la del ${fecha(d.fecha_anterior)}` : "") + " · puntajes informativos"))));
  const botones = el("div", { class: "grupo", role: "group", "aria-label": "Screeners predefinidos" });
  const sectores = [...new Set(d.filas.map((r) => r.sector_sfc).filter(Boolean))].sort();
  const selSector = el("select", { "aria-label": "Sector", style: { height: "32px", background: "var(--fondo)", border: "1px solid var(--borde-boton)", borderRadius: "6px" }, onchange: (e) => { sector = e.target.value; pintar(); } },
    el("option", { value: "" }, "Todos los sectores"), sectores.map((s) => el("option", { value: s }, s)));
  const chkU = el("input", { type: "checkbox", checked: true, onchange: (e) => { soloUniverso = e.target.checked; pintar(); } });
  const minP = el("input", { type: "number", min: 1, max: 10, step: 0.5, value: "", placeholder: "mín.", style: { width: "70px", height: "32px", background: "var(--fondo)", border: "1px solid var(--borde-boton)", borderRadius: "6px", padding: "0 6px" }, oninput: (e) => { minimo = +e.target.value || 0; pintar(); } });
  const descripcion = el("p", { class: "desc" });
  const caja = el("div");
  const cols = [
    { k: "nemotecnico", t: "Acción", izq: true, f: (r) => el("a", { href: "#/ficha/" + r.especie, style: { fontWeight: 600 } }, r.nemotecnico || r.nombre_corto || r.especie), v: (r) => r.nemotecnico || r.especie },
    { k: "sector_sfc", t: "Sector", izq: true, f: (r) => (r.sector_sfc || "—").slice(0, 28) },
    { k: "puntaje_global", t: "Global", f: (r) => notaPuntaje(r.puntaje_global), info: "puntaje_global" },
    { k: "puntaje_valor", t: "Valor", f: (r) => notaPuntaje(r.puntaje_valor) },
    { k: "puntaje_calidad", t: "Calidad", f: (r) => notaPuntaje(r.puntaje_calidad) },
    { k: "puntaje_crecimiento", t: "Crecim.", f: (r) => notaPuntaje(r.puntaje_crecimiento) },
    { k: "puntaje_momentum", t: "Momentum", f: (r) => notaPuntaje(r.puntaje_momentum) },
    { k: "puntaje_riesgo", t: "Riesgo", f: (r) => notaPuntaje(r.puntaje_riesgo) },
    { k: "p_vl", t: "P/VL", f: (r) => veces(r.p_vl), info: "p_vl" },
    { k: "p_u", t: "P/U", f: (r) => veces(r.p_u, 1), info: "p_u" },
    { k: "rendimiento_dividendo_12m", t: "Div. 12m", f: (r) => pct(r.rendimiento_dividendo_12m), info: "rendimiento_dividendo_12m" },
    { k: "roe_12m", t: "ROE", f: (r) => pct(r.roe_12m), info: "roe_12m" },
    { k: "momentum_12_1", t: "Mom. 12-1", f: (r) => pct(r.momentum_12_1), info: "momentum_12_1" },
    { k: "capitalizacion", t: "Capitalización", f: (r) => pesos(r.capitalizacion) },
    { k: "tercio_validado", t: "Validados", f: (r) => r.tercio_validado || "—", info: "percentil_validado" },
  ];
  const pintar = () => {
    let filas = d.filas;
    if (elegido) { const s = d.screeners.find((x) => x.id === elegido); filas = filas.filter((r) => s.especies.includes(r.especie)); }
    if (sector) filas = filas.filter((r) => r.sector_sfc === sector);
    if (soloUniverso && !elegido) filas = filas.filter((r) => r.en_universo);
    if (minimo) filas = filas.filter((r) => (r.puntaje_global ?? 0) >= minimo);
    const s = d.screeners.find((x) => x.id === elegido);
    descripcion.replaceChildren(s ? `${s.descripcion}. Entraron: ${s.nuevas.length ? s.nuevas.join(", ") : "ninguna"}. Salieron: ${s.salieron.length ? s.salieron.join(", ") : "ninguna"}.` : "Todas las acciones con puntaje. Clic en un encabezado para ordenar; clic en la acción para abrir su ficha.");
    caja.replaceChildren(tabla(cols, filas, { orden: "puntaje_global" }));
    botones.replaceChildren(el("button", { type: "button", "aria-pressed": String(!elegido), onclick: () => { elegido = null; pintar(); } }, "Todas"),
      ...d.screeners.map((x) => el("button", { type: "button", "aria-pressed": String(elegido === x.id), onclick: () => { elegido = x.id; pintar(); } }, `${x.nombre} (${x.especies.length})`)));
    exportar.onclick = () => csv(`screener_${elegido || "todas"}_${d.fecha}.csv`, cols, filas);
  };
  const exportar = el("button", { type: "button", class: "boton" }, "Exportar CSV");
  v.append(el("section", { class: "panel" },
    botones,
    el("div", { class: "encabezado" }, el("div", { style: { display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap" } }, selSector,
      el("label", { style: { fontSize: "12px", color: "var(--tenue)", display: "flex", gap: "6px", alignItems: "center" } }, chkU, "Solo universo líquido"),
      el("label", { style: { fontSize: "12px", color: "var(--tenue)", display: "flex", gap: "6px", alignItems: "center" } }, "Puntaje global", minP)), exportar),
    descripcion, caja));
  pintar();
};

paginas.matriz = async (v) => {
  const d = await api("/api/matriz");
  const informativa = d.filas.some((r) => r.estado === "informativo");
  v.append(el("div", {}, el("h1", {}, "Matriz de decisión"),
    el("div", { class: "sub" }, informativa ? "Informativa: los factores de la matriz todavía no pasan la validación estadística, así que no se traduce a Comprar, Acumular, Vigilar o Evitar." : "Validada")));
  const caja = el("div");
  const TERCIO = { superior: "#A9C2FF", medio: "#6E9BFF", inferior: "#2A4A8C" };
  v.append(el("div", { class: "fila" },
    el("section", { class: "panel ancho" }, el("h2", {}, "Fundamental × técnico · 1 a 10", info("cuadrante")), caja,
      leyenda([[TERCIO.superior, "Tercio superior de los factores validados"], [TERCIO.medio, "Tercio medio"], [TERCIO.inferior, "Tercio inferior"], ["#5B6B8C", "Fuera del universo"]]),
      el("p", { class: "desc" }, "El eje técnico queda vacío (y la acción no aparece) cuando la acción no es líquida. El color muestra lo que sí tiene respaldo estadístico: el tercio según momentum 12-1 y rendimiento por dividendo.")),
    panel("Evidencia de los factores",
      tabla([
        { k: "factor", t: "Factor", izq: true },
        { k: "veredicto", t: "Veredicto", izq: true, f: (r) => el("span", { class: r.veredicto === "validado" ? "estado-ok" : "estado-medio" }, r.veredicto.replace("_", " ")) },
        { k: "ic_medio", t: "IC", f: (r) => num(r.ic_medio, 3) },
        { k: "ic_t", t: "t", f: (r) => num(r.ic_t, 1) },
        { k: "exceso_superior_neto", t: "Exceso neto", f: (r) => pct(r.exceso_superior_neto, 2) },
      ], d.validacion),
      el("a", { href: "/tablero", target: "_blank", style: { color: "var(--acento)", fontWeight: 600, fontSize: "13px" } }, "Abrir el tablero de los factores validados →"))));
  requestAnimationFrame(() => dispersion(caja, d.filas.filter((r) => r.puntaje_tecnico != null).map((r) => ({
    x: r.puntaje_fundamental, y: r.puntaje_tecnico, etiqueta: r.nemotecnico || r.emisor_corto || r.especie,
    color: r.en_universo ? (TERCIO[r.tercio_validado] || "#6E9BFF") : "#5B6B8C", href: "#/ficha/" + r.especie,
    filas: [["Fundamental", num(r.puntaje_fundamental, 1)], ["Técnico", num(r.puntaje_tecnico, 1)], ["Global", num(r.puntaje_global, 1)],
      ["Cuadrante", CUADRANTE[r.cuadrante] || "—"], ["Factores validados", r.tercio_validado || "—"]],
  })), { alto: 480, titulo: "Matriz fundamental por técnico", ejeX: "Puntaje fundamental (valor, calidad, crecimiento)", ejeY: "Puntaje técnico (momentum)",
    cuadrantes: [[9.9, 9.7, "end", "ambos altos"], [1.1, 9.7, "start", "técnico alto"], [9.9, 1.3, "end", "fundamental alto"], [1.1, 1.3, "start", "ambos bajos"]] }));
};

paginas.ficha = async (v, especie) => {
  if (!especie) {
    v.append(el("h1", {}, "Ficha"), el("div", { class: "vacio" }, "Busque una acción arriba o elíjala en el screener o el mapa."));
    return;
  }
  const d = await api("/api/ficha/" + encodeURIComponent(especie));
  const c = d.cabecera;
  if (d.sin_puntaje) {
    v.append(el("h1", {}, c.nombre_corto || c.nombre), el("div", { class: "vacio" }, "Esta especie no tiene datos recientes para su ficha (sin precio en el último mes)."));
    return;
  }
  const factores = Object.fromEntries(d.auditoria.filter((a) => a.nivel !== "metrica").map((a) => [a.elemento, a]));
  const cab = el("section", { class: "panel", style: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: "12px 28px" } },
    el("div", {}, el("div", { style: { display: "flex", alignItems: "center", gap: "10px" } }, el("h1", {}, c.nombre_corto || c.emisor_nombre), c.nemotecnico ? el("span", { class: "chip gris" }, c.nemotecnico) : null),
      el("div", { class: "sub" }, `${c.sector_sfc || "Sin sector"} · ${c.clase_accion || ""} · bursatilidad ${c.iba_categoria || "—"}${c.en_universo ? " · en el universo líquido" : " · fuera del universo"}`)),
    el("div", { style: { display: "flex", alignItems: "baseline", gap: "8px" } }, el("span", { style: { fontSize: "26px", fontWeight: 700 } }, num(c.precio, c.precio < 100 ? 2 : 0)), el("span", { class: "nota" }, "COP al " + fecha(c.fecha))),
    el("div", { style: { display: "flex", alignItems: "center", gap: "8px" } }, el("span", { class: "nota-puntaje " + banda(c.puntaje_global), style: { fontSize: "17px", padding: "4px 10px" } }, c.puntaje_global == null ? "N/D" : num(c.puntaje_global, 1)),
      el("span", { class: "nota" }, "global de 10", el("br"), "vs " + (c.grupo_comparacion === "universo" ? "el universo" : c.grupo_comparacion)), info("puntaje_global")),
    c.cuadrante ? el("span", { class: "chip" }, (c.decision || CUADRANTE[c.cuadrante]) + (c.estado === "informativo" ? " · informativo" : "")) : null,
    el("div", { style: { display: "flex", flexWrap: "wrap", gap: "6px" } }, d.avisos.map((a) => el("span", { class: "aviso" }, a))));
  v.append(cab);

  const barras = ["valor", "calidad", "crecimiento", "momentum", "riesgo"].map((f) => {
    const a = factores[f]; const p = a?.puntaje;
    return el("div", { class: "barra-factor" }, el("span", {}, f[0].toUpperCase() + f.slice(1)),
      el("div", { class: "riel" }, el("div", { style: { width: p == null ? "0" : p * 10 + "%", background: COLOR_BANDA[banda(p)] } })), notaPuntaje(p));
  });
  const cajaPrecio = el("div");
  let dias = 365;
  const periodos = el("div", { class: "grupo", role: "group", "aria-label": "Periodo" });
  const pintarPrecio = async () => {
    periodos.replaceChildren(...[[182, "6M"], [365, "1A"], [1095, "3A"], [1825, "5A"]].map(([n, t]) => el("button", { type: "button", "aria-pressed": String(dias === n), onclick: () => { dias = n; pintarPrecio(); } }, t)));
    const serie = await api(`/api/ficha/${encodeURIComponent(especie)}/precio?dias=${dias}`);
    lineas(cajaPrecio, [
      { nombre: "Precio ajustado", color: "#E6EAF2", puntos: serie.map((r) => ({ x: r.fecha, y: r.precio_ajustado })), grosor: 2.25 },
      { nombre: "Media de 200", color: "#5B6B8C", discontinua: true, puntos: serie.map((r) => ({ x: r.fecha, y: r.sma200 })) },
    ], { alto: 280, titulo: "Precio ajustado por dividendos y su media de 200 sesiones", fy: (x) => num(x, x < 100 ? 2 : 0) });
  };
  const t = d.tecnico || {};
  v.append(el("div", { class: "fila" },
    el("div", { style: { display: "flex", flexDirection: "column", gap: "16px" } },
      panel("Puntaje por factor · 1 a 10", ...barras,
        leyenda([["#3DD68C", "8–10 Fuerte"], ["#6E9BFF", "6–7,9 Bueno"], ["#F5B54A", "4–5,9 Neutral"], ["#FF7A6B", "1–3,9 Débil"]]),
        el("p", { class: "desc" }, "Un factor sin datos queda en N/D y no cuenta en el global.")),
      panel("Técnico", ...[["Distancia a la media de 200", pct(t.distancia_sma200), "distancia_sma200"], ["RSI 14", num(t.rsi_14, 0), "rsi_14"],
        ["Caída desde máx. 52 sem.", pct(t.caida_desde_max_52s), "caida_desde_max_52s"], ["Retorno 12 meses", pct(t.retorno_12m), "retorno_12m"],
        ["Fuerza relativa 12 meses", pct(t.fuerza_relativa_12m), "fuerza_relativa_12m"]].map(([n, val, id]) =>
        el("div", { class: "evento", style: { justifyContent: "space-between" } }, el("span", {}, n, info(id, val)), el("strong", {}, val))),
        t.tecnico_valido === false ? el("p", { class: "desc" }, "Acción poco líquida: el técnico es informativo (N/A en la matriz).") : null)),
    el("section", { class: "panel ancho" }, el("div", { class: "encabezado" }, el("h2", {}, "Precio"), periodos), cajaPrecio,
      leyenda([["#E6EAF2", "Precio ajustado por dividendos", "linea"], ["#5B6B8C", "Media de 200 sesiones", "linea"]]))));
  requestAnimationFrame(pintarPrecio);

  const metricas = d.auditoria.filter((a) => a.nivel === "metrica");
  v.append(el("div", { class: "fila" },
    panel("Métricas del puntaje",
      tabla([
        { k: "variable", t: "Métrica", izq: true, f: (r) => el("span", { style: { display: "inline-flex", alignItems: "center" } }, r.variable || r.elemento, info(r.elemento, segunFormato(r.valor, r.formato))) },
        { k: "factor", t: "Factor", izq: true },
        { k: "valor", t: "Valor", f: (r) => segunFormato(r.valor, r.formato) },
        { k: "puntaje", t: "Puntaje", f: (r) => notaPuntaje(r.valor == null ? null : r.puntaje) },
        { k: "pares", t: "Pares", f: (r) => r.pares ?? "—" },
      ], metricas),
      el("p", { class: "desc" }, "Puntaje = 5,5 + 1,5 × z frente a la mediana del grupo, entre 1 y 10.")),
    panel("Riesgo (fin de mes)",
      tabla([
        { k: "ventana", t: "Ventana", izq: true }, { k: "moneda", t: "Moneda", izq: true },
        { k: "retorno_anual", t: "Retorno anual", f: (r) => pct(r.retorno_anual) },
        { k: "volatilidad_anual", t: "Volatilidad", f: (r) => pct(r.volatilidad_anual, 0), info: "volatilidad_anual" },
        { k: "beta", t: "Beta", f: (r) => num(r.beta, 2), info: "beta" },
        { k: "sharpe", t: "Sharpe", f: (r) => num(r.sharpe, 2), info: "sharpe" },
        { k: "drawdown_maximo", t: "Caída máx.", f: (r) => pct(r.drawdown_maximo, 0), info: "drawdown_maximo" },
        { k: "cvar_95", t: "CVaR 95 %", f: (r) => pct(r.cvar_95), info: "cvar_95" },
      ], d.riesgo))));

  const TIPOS = { dividendo_ex: "Fecha ex", dividendo_pago: "Pago", asamblea_convocatoria: "Convocatoria", asamblea_decision: "Asamblea" };
  v.append(el("div", { class: "fila" },
    panel("¿Por qué se mueve? · información relevante", d.eventos.length ? d.eventos.map((e) => el("div", { class: "evento" }, el("span", { class: "f" }, fecha(e.fecha)), el("span", {}, (e.resumen || e.tema || "").slice(0, 220)))) : el("div", { class: "vacio" }, "Sin eventos.")),
    panel("Dividendos y calendario", d.calendario.length ? tabla([
      { k: "fecha", t: "Fecha", izq: true, f: (r) => fecha(r.fecha) + (r.estimada ? " (est.)" : "") },
      { k: "tipo", t: "Tipo", izq: true, f: (r) => TIPOS[r.tipo] || r.tipo },
      { k: "valor", t: "Valor", f: (r) => r.valor == null ? "" : num(r.valor, 2) + " " + (r.moneda || "") },
      { k: "descripcion", t: "Detalle", izq: true, f: (r) => (r.descripcion || "").slice(0, 60) },
    ], d.calendario) : el("div", { class: "vacio" }, "Sin dividendos registrados en el último año."))));

  const colsPares = [
    { k: "nemotecnico", t: "Acción", izq: true, f: (r) => el("a", { href: "#/ficha/" + r.especie, style: { fontWeight: r.especie === especie ? 700 : 500 } }, r.nemotecnico || r.emisor_corto || r.especie), v: (r) => r.nemotecnico || r.especie },
    { k: "precio", t: "Precio", f: (r) => num(r.precio, r.precio < 100 ? 2 : 0) },
    { k: "p_vl", t: "P/VL", f: (r) => veces(r.p_vl) }, { k: "p_u", t: "P/U", f: (r) => veces(r.p_u, 1) },
    { k: "roe_12m", t: "ROE", f: (r) => pct(r.roe_12m) }, { k: "rendimiento_dividendo_12m", t: "Div. 12m", f: (r) => pct(r.rendimiento_dividendo_12m) },
    { k: "en_universo", t: "Líquida", f: (r) => r.en_universo ? "sí" : "no" },
    { k: "puntaje_global", t: "Puntaje", f: (r) => notaPuntaje(r.puntaje_global) },
  ];
  v.append(el("section", { class: "panel" }, el("div", { class: "encabezado" }, el("h2", {}, "Comparación con su sector"),
    el("button", { type: "button", class: "boton", onclick: () => csv(`pares_${especie}.csv`, colsPares, d.pares) }, "Exportar CSV")), tabla(colsPares, d.pares)));
};

// ------------------------------------------------------------------ cartera
function elegirCartera(v, alCambiar) {
  const carteras = estado.info?.carteras || [];
  if (!carteras.length) return null;
  let actual = localStorage.getItem("cartera") || carteras[0].cartera_id;
  if (!carteras.some((c) => c.cartera_id === actual)) actual = carteras[0].cartera_id;
  const sel = el("select", { "aria-label": "Cartera", style: { height: "40px", background: "var(--panel)", border: "1px solid var(--borde-boton)", borderRadius: "8px", padding: "0 10px" },
    onchange: (e) => { localStorage.setItem("cartera", e.target.value); alCambiar(e.target.value); } },
    carteras.map((c) => el("option", { value: c.cartera_id }, `${c.nombre} (${c.tipo})`)));
  sel.value = actual;
  return { sel, actual };
}

paginas.cartera = async (v) => {
  const eleccion = elegirCartera(v, () => ir());
  v.append(el("div", { class: "encabezado" }, el("div", {}, el("h1", {}, "Mi cartera"), el("div", { class: "sub" }, "Valores en pesos, con el último precio disponible")),
    el("div", { style: { display: "flex", gap: "8px" } }, eleccion ? eleccion.sel : null, el("a", { class: "boton", href: "#/carteras", style: { display: "inline-flex", alignItems: "center" } }, "Registrar transacción"))));
  if (!eleccion) { v.append(panel(null, el("div", { class: "vacio" }, "Todavía no hay carteras. Cree una en ", el("a", { href: "#/carteras", style: { color: "var(--acento)" } }, "Carteras y transacciones"), "."))); return; }
  const d = await api("/api/cartera/" + encodeURIComponent(eleccion.actual));
  if (d.vacia) { v.append(panel(null, el("div", { class: "vacio" }, "Esta cartera no tiene transacciones todavía."))); return; }
  const total = d.posiciones.reduce((a, p) => a + (p.valor_cop || 0), 0);
  const ganancia = d.posiciones.reduce((a, p) => a + (p.ganancia_no_realizada_cop || 0), 0);
  const ult = d.desempeno[d.desempeno.length - 1] || {};
  const anual = d.dividendos.filter((x) => x.fecha <= new Date(Date.now() + 365 * 864e5).toISOString().slice(0, 10)).reduce((a, x) => a + (x.total || 0), 0);
  v.append(el("section", { class: "indicadores" },
    kpi("Valor", pesos(total), "al " + fecha(d.fecha)),
    kpi("Ganancia no realizada", pesos(ganancia), null, ganancia >= 0 ? "sube" : "baja"),
    kpi("TWR acumulado", pct(ult.twr_acumulado), ult.retorno_benchmark != null ? "mes: " + pct(ult.retorno_mes) + " · bench. " + pct(ult.retorno_benchmark) : null),
    kpi("XIRR desde el inicio", pct(ult.xirr_desde_inicio), "anual efectivo"),
    kpi("Dividendos por cobrar (12 m)", pesos(anual), d.dividendos.length + " pagos anunciados")));
  const cajaMapa = el("div"), cajaValor = el("div");
  const filasDesvio = d.asignacion.filter((a) => a.peso_objetivo != null || a.peso > 0).map((a) => {
    const desvio = a.desvio ?? null;
    return el("div", { class: "barra-factor", style: { gridTemplateColumns: "130px minmax(0,1fr) 64px" } }, el("span", {}, a.clase_activo_id),
      el("div", { class: "riel", style: { position: "relative", height: "14px", background: "var(--linea)" } },
        el("div", { style: { position: "absolute", left: "50%", top: "-2px", width: "1px", height: "18px", background: "#5B6B8C" } }),
        desvio == null ? null : el("div", { style: { position: "absolute", top: "3px", height: "8px", left: desvio < 0 ? (50 + Math.max(desvio * 100, -50)) + "%" : "50%", width: Math.min(Math.abs(desvio * 100), 50) + "%", background: a.fuera_de_banda ? "#FF7A6B" : "#6E9BFF", borderRadius: "2px" } })),
      el("span", { style: { textAlign: "right", fontWeight: 600, color: a.fuera_de_banda ? "var(--debil-texto)" : "var(--texto)" } }, desvio == null ? pct(a.peso, 0) : (desvio >= 0 ? "+" : "") + num(desvio * 100, 1)));
  });
  v.append(el("div", { class: "fila" },
    el("section", { class: "panel ancho" }, el("h2", {}, "Posiciones · tamaño = peso, color = retorno 12 meses"), cajaMapa),
    el("div", { style: { display: "flex", flexDirection: "column", gap: "16px" } },
      panel("Desvío frente al objetivo (puntos)", filasDesvio.length ? filasDesvio : el("div", { class: "vacio" }, "El perfil de esta cartera no tiene asignación objetivo."),
        el("p", { class: "desc" }, "Rojo = fuera de banda.")),
      panel("Próximos dividendos", d.dividendos.length ? d.dividendos.slice(0, 8).map((x) => el("div", { class: "evento" }, el("span", { class: "f" }, fecha(x.fecha)), el("span", {}, `${x.nombre_corto}: ${pesos(x.total)}`))) : el("div", { class: "vacio" }, "Sin pagos anunciados.")))));
  v.append(panel("Valor de la cartera", cajaValor));
  v.append(panel("Posiciones", tabla([
    { k: "especie", t: "Activo", izq: true, f: (r) => r.especie === "EFECTIVO" ? "Efectivo" : el("a", { href: "#/ficha/" + r.especie, style: { fontWeight: 600 } }, r.nemotecnico || r.nombre_corto || r.especie) },
    { k: "cantidad", t: "Cantidad", f: (r) => r.especie === "EFECTIVO" ? "" : num(r.cantidad, 0) },
    { k: "precio", t: "Precio", f: (r) => r.especie === "EFECTIVO" ? "" : num(r.precio, 0) },
    { k: "costo_fiscal_cop", t: "Costo fiscal", f: (r) => pesos(r.costo_fiscal_cop) },
    { k: "valor_cop", t: "Valor", f: (r) => pesos(r.valor_cop) },
    { k: "peso", t: "Peso", f: (r) => pct(r.peso) },
    { k: "ganancia_no_realizada_cop", t: "Ganancia", f: (r) => el("span", { class: r.ganancia_no_realizada_cop >= 0 ? "sube" : "baja" }, pesos(r.ganancia_no_realizada_cop)) },
    { k: "puntaje_global", t: "Puntaje", f: (r) => r.especie === "EFECTIVO" ? "" : notaPuntaje(r.puntaje_global) },
  ], d.posiciones, { orden: "valor_cop" })));
  requestAnimationFrame(() => {
    mapaArbol(cajaMapa, d.posiciones.map((p) => ({ valor: p.valor_cop, etiqueta: p.especie === "EFECTIVO" ? "Efectivo" : (p.nemotecnico || p.nombre_corto || p.especie),
      sub: pct(p.peso) + (p.retorno_12m != null ? " · " + pct(p.retorno_12m) : ""), r: p, href: p.especie === "EFECTIVO" ? null : "#/ficha/" + p.especie,
      filas: [["Valor", pesos(p.valor_cop)], ["Peso", pct(p.peso)], ["Retorno 12 meses", pct(p.retorno_12m)], ["Puntaje", num(p.puntaje_global, 1)]] })),
    { alto: 340, titulo: "Posiciones de la cartera", color: (it) => it.r.especie === "EFECTIVO" ? "#2A3448" : divergente(it.r.retorno_12m, 0.3) });
    lineas(cajaValor, [{ nombre: "Valor", color: "#6E9BFF", puntos: d.valor.map((r) => ({ x: r.fecha, y: r.valor })) }], { alto: 220, fy: pesos, titulo: "Valor diario de la cartera" });
  });
};

// Formularios genéricos sobre /api/usuario/<tabla>
function formulario(campos, alGuardar, inicial = {}) {
  const f = el("form", { class: "formulario" });
  const mensaje = el("span", { class: "mensaje" });
  for (const c of campos) {
    let control;
    if (c.opciones) control = el("select", { name: c.k }, c.opciones.map((o) => el("option", { value: Array.isArray(o) ? o[0] : o }, Array.isArray(o) ? o[1] : o)));
    else if (c.tipo === "textarea") control = el("textarea", { name: c.k });
    else control = el("input", { name: c.k, type: c.tipo || "text", step: c.tipo === "number" ? "any" : null, list: c.lista || null, placeholder: c.ayuda || "" });
    if (inicial[c.k] != null) control.value = inicial[c.k];
    f.append(el("label", {}, c.t + (c.obligatorio ? " *" : ""), control));
  }
  f.append(el("div", { class: "acciones" }, el("button", { type: "submit", class: "boton primario" }, "Guardar"), mensaje));
  f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const datos = Object.fromEntries(new FormData(f).entries());
    mensaje.className = "mensaje"; mensaje.textContent = "Guardando…";
    try {
      const r = await alGuardar(datos);
      if (r.ok) { mensaje.className = "mensaje ok"; mensaje.textContent = "Guardado."; await refrescarEstado(); setTimeout(ir, 500); }
      else { mensaje.className = "mensaje error"; mensaje.textContent = "No se guardó: " + r.problemas.join("; "); }
    } catch (e) { mensaje.className = "mensaje error"; mensaje.textContent = "Error: " + e.message; }
  });
  return f;
}
const hoyIso = () => new Date().toISOString().slice(0, 10);
const listaActivos = "lista-activos";

paginas.carteras = async (v) => {
  v.append(el("div", {}, el("h1", {}, "Carteras y transacciones"), el("div", { class: "sub" }, "Se guarda en invest-data/usuario/ (repositorio privado) y se valida antes de escribir")));
  const carteras = await api("/api/usuario/dim_cartera");
  const perfiles = estado.info?.perfiles || [];
  v.append(el("div", { class: "fila" },
    panel("Carteras", carteras.filas.length ? tabla([
      { k: "nombre", t: "Nombre", izq: true }, { k: "tipo", t: "Tipo", izq: true }, { k: "perfil_id", t: "Perfil", izq: true },
      { k: "moneda_base", t: "Moneda", izq: true }, { k: "intermediario", t: "Intermediario", izq: true },
    ], carteras.filas) : el("div", { class: "vacio" }, "Sin carteras.")),
    panel("Nueva cartera", formulario([
      { k: "nombre", t: "Nombre", obligatorio: true }, { k: "tipo", t: "Tipo", opciones: [["real", "Real"], ["hipotetica", "Hipotética"]] },
      { k: "perfil_id", t: "Perfil", opciones: [["", "(sin perfil)"], ...perfiles.map((p) => [p.perfil_id, p.nombre])] },
      { k: "moneda_base", t: "Moneda base", opciones: ["COP", "USD"] }, { k: "intermediario", t: "Intermediario" },
    ], (d) => postear("/api/usuario/dim_cartera", { filas: [{ ...d, cartera_id: d.nombre.toLowerCase().normalize("NFD").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, ""), creada: hoyIso() }] })))));
  const eleccion = elegirCartera(v, () => ir());
  if (!eleccion) return;
  const trans = await api("/api/usuario/fact_transaccion");
  const mias = trans.filas.filter((t) => t.cartera_id === eleccion.actual).sort((a, b) => (b.fecha_operacion || "").localeCompare(a.fecha_operacion || ""));
  v.append(el("div", { class: "encabezado" }, el("h1", { style: { fontSize: "18px" } }, "Transacciones"), eleccion.sel));
  v.append(panel("Registrar transacción", formulario([
    { k: "fecha_operacion", t: "Fecha de operación", tipo: "date", obligatorio: true }, { k: "fecha_liquidacion", t: "Fecha de liquidación", tipo: "date" },
    { k: "tipo", t: "Tipo", opciones: [["compra", "Compra"], ["venta", "Venta"], ["dividendo", "Dividendo en efectivo"], ["dividendo_acciones", "Dividendo en acciones"], ["aporte", "Aporte"], ["retiro", "Retiro"], ["comision", "Comisión"], ["impuesto", "Impuesto"]] },
    { k: "especie", t: "Especie (si aplica)", lista: listaActivos, ayuda: "p. ej. coc04paao008" }, { k: "cantidad", t: "Cantidad", tipo: "number" },
    { k: "precio", t: "Precio", tipo: "number" }, { k: "monto", t: "Monto (si no es cantidad × precio)", tipo: "number" },
    { k: "comision", t: "Comisión", tipo: "number" }, { k: "gmf", t: "GMF (4 × 1.000)", tipo: "number" }, { k: "retencion", t: "Retención", tipo: "number" },
    { k: "moneda", t: "Moneda", opciones: ["COP", "USD"] }, { k: "trm", t: "TRM aplicada (si es USD)", tipo: "number" }, { k: "notas", t: "Notas" },
  ], (d) => postear("/api/usuario/fact_transaccion", { filas: [{ ...d, especie: (d.especie || "").split(" ")[0] || null, cartera_id: eleccion.actual }] }), { fecha_operacion: hoyIso() })));
  v.append(panel("Historial", mias.length ? tabla([
    { k: "fecha_operacion", t: "Fecha", izq: true, f: (r) => fecha(r.fecha_operacion) }, { k: "tipo", t: "Tipo", izq: true }, { k: "especie", t: "Especie", izq: true },
    { k: "cantidad", t: "Cantidad", f: (r) => num(r.cantidad, 0) }, { k: "precio", t: "Precio", f: (r) => num(r.precio, 2) },
    { k: "monto", t: "Monto", f: (r) => pesos(r.monto ?? (r.cantidad && r.precio ? r.cantidad * r.precio : null)) },
    { k: "comision", t: "Comisión", f: (r) => pesos(r.comision) },
    { k: "x", t: "", f: (r) => el("button", { type: "button", class: "boton", style: { height: "30px" }, onclick: async () => {
      if (!confirm("¿Borrar esta transacción?")) return;
      await postear("/api/usuario/fact_transaccion", { borrar: [{ transaccion_id: r.transaccion_id }] }); ir(); } }, "Borrar") },
  ], mias) : el("div", { class: "vacio" }, "Sin transacciones."),
  el("p", { class: "desc" }, "Para cargar muchas desde el extracto del comisionista: invest usuario --importar fact_transaccion archivo.csv (plantilla en invest-data/usuario/plantillas/).")));
};

paginas.perfiles = async (v) => {
  const perfiles = await api("/api/usuario/dim_perfil");
  const asig = await api("/api/usuario/dim_perfil_asignacion");
  v.append(el("div", {}, el("h1", {}, "Perfiles de inversionista"), el("div", { class: "sub" }, "Editar un perfil crea una versión nueva; las carteras usan la versión vigente en cada fecha")));
  const ultimos = Object.values(perfiles.filas.reduce((a, p) => { if (!a[p.perfil_id] || a[p.perfil_id].version < p.version) a[p.perfil_id] = p; return a; }, {}));
  v.append(panel("Perfiles", ultimos.length ? tabla([
    { k: "nombre", t: "Nombre", izq: true }, { k: "version", t: "Versión" }, { k: "objetivo", t: "Objetivo", izq: true },
    { k: "horizonte_anios", t: "Horizonte (años)" }, { k: "moneda_base", t: "Moneda", izq: true },
    { k: "perdida_maxima_aceptable", t: "Pérdida máx.", f: (r) => pct(r.perdida_maxima_aceptable, 0) },
    { k: "meta_retorno_real", t: "Meta real", f: (r) => pct(r.meta_retorno_real, 1) }, { k: "benchmark", t: "Benchmark", izq: true },
    { k: "vigente_desde", t: "Desde", f: (r) => fecha(r.vigente_desde) },
  ], ultimos) : el("div", { class: "vacio" }, "Sin perfiles.")));
  const campos = [
    { k: "perfil_id", t: "Identificador (sin espacios)", obligatorio: true }, { k: "nombre", t: "Nombre", obligatorio: true },
    { k: "objetivo", t: "Objetivo", opciones: [["crecimiento", "Crecimiento"], ["renta", "Renta"], ["preservacion", "Preservación"]] },
    { k: "horizonte_anios", t: "Horizonte (años)", tipo: "number" }, { k: "moneda_base", t: "Moneda base", opciones: ["COP", "USD"] },
    { k: "perdida_maxima_aceptable", t: "Pérdida máxima aceptable (0,25 = 25 %)", tipo: "number" }, { k: "volatilidad_objetivo", t: "Volatilidad objetivo", tipo: "number" },
    { k: "meta_retorno_real", t: "Meta de retorno real", tipo: "number" }, { k: "liquidez_reserva", t: "Liquidez de reserva", tipo: "number" },
    { k: "maximo_por_emisor", t: "Máximo por emisor", tipo: "number" },
    { k: "benchmark", t: "Benchmark", opciones: [["universo_igual", "Universo líquido (pesos iguales)"], ["universo_cap", "Universo por capitalización"]] },
    { k: "excluidos", t: "Activos excluidos" }, { k: "notas", t: "Notas" },
  ];
  v.append(panel("Crear o editar (nueva versión)", formulario(campos, (d) => {
    const previas = perfiles.filas.filter((p) => p.perfil_id === d.perfil_id);
    const version = previas.length ? Math.max(...previas.map((p) => p.version)) + 1 : 1;
    const cerrar = previas.filter((p) => !p.vigente_hasta).map((p) => ({ ...p, vigente_hasta: new Date(Date.now() - 864e5).toISOString().slice(0, 10) }));
    return postear("/api/usuario/dim_perfil", { filas: [...cerrar, { ...d, version, vigente_desde: hoyIso() }] });
  })));
  const perfilSel = el("select", { "aria-label": "Perfil", style: { height: "36px", background: "var(--fondo)", border: "1px solid var(--borde-boton)", borderRadius: "8px" } }, ultimos.map((p) => el("option", { value: p.perfil_id + "|" + p.version }, `${p.nombre} v${p.version}`)));
  if (!ultimos.length) return;
  const cajaAsig = el("div");
  const pintarAsig = () => {
    const [pid, ver] = perfilSel.value.split("|");
    const filas = asig.filas.filter((a) => a.perfil_id === pid && String(a.version) === ver);
    const suma = filas.reduce((a, x) => a + (x.peso_objetivo || 0), 0);
    cajaAsig.replaceChildren(filas.length ? tabla([
      { k: "clase_activo_id", t: "Clase", izq: true }, { k: "pais_id", t: "País", izq: true }, { k: "moneda_id", t: "Moneda", izq: true },
      { k: "peso_objetivo", t: "Objetivo", f: (r) => pct(r.peso_objetivo, 0) }, { k: "banda_minima", t: "Mín.", f: (r) => pct(r.banda_minima, 0) }, { k: "banda_maxima", t: "Máx.", f: (r) => pct(r.banda_maxima, 0) },
    ], filas) : el("div", { class: "vacio" }, "Sin asignación para esta versión."),
    el("p", { class: "desc" }, `Suma de objetivos: ${pct(suma, 0)}` + (Math.abs(suma - 1) > 0.001 ? " (debería ser 100 %)" : "")),
    formulario([
      { k: "clase_activo_id", t: "Clase", opciones: [["ACCION", "Acciones"], ["EFECTIVO", "Efectivo"], ["ETF", "ETF"], ["TES", "TES"], ["CDT", "CDT"], ["FIC", "FIC"], ["BONO", "Bonos"]] },
      { k: "pais_id", t: "País", opciones: ["CO", "US", "GLOBAL"] }, { k: "moneda_id", t: "Moneda", opciones: ["COP", "USD"] },
      { k: "peso_objetivo", t: "Peso objetivo (0,6 = 60 %)", tipo: "number", obligatorio: true }, { k: "banda_minima", t: "Banda mínima", tipo: "number" }, { k: "banda_maxima", t: "Banda máxima", tipo: "number" },
    ], (d) => postear("/api/usuario/dim_perfil_asignacion", { filas: [{ ...d, perfil_id: pid, version: +ver }] })));
  };
  perfilSel.addEventListener("change", pintarAsig);
  v.append(el("section", { class: "panel" }, el("div", { class: "encabezado" }, el("h2", {}, "Asignación objetivo"), perfilSel), cajaAsig));
  pintarAsig();
};

paginas.tesis = async (v) => {
  v.append(el("div", {}, el("h1", {}, "Tesis de inversión"), el("div", { class: "sub" }, "Una ficha por posición: por qué la tiene, qué la confirmaría y qué la invalidaría")));
  const eleccion = elegirCartera(v, () => ir());
  if (!eleccion) { v.append(panel(null, el("div", { class: "vacio" }, "Primero cree una cartera."))); return; }
  const t = await api("/api/usuario/dim_tesis");
  const mias = t.filas.filter((x) => x.cartera_id === eleccion.actual);
  v.append(el("div", { class: "encabezado" }, el("span"), eleccion.sel));
  v.append(panel("Tesis registradas", mias.length ? mias.map((x) => el("div", { class: "panel", style: { background: "var(--panel2)" } },
    el("div", { class: "encabezado" }, el("a", { href: "#/ficha/" + x.especie, style: { fontWeight: 700 } }, x.especie),
      el("span", { class: "nota" }, "Revisar el " + fecha(x.fecha_revision) + (x.fecha_revision && x.fecha_revision < hoyIso() ? " · vencida" : ""))),
    el("p", { style: { margin: 0 } }, x.tesis),
    el("div", { class: "nota" }, `Catalizador: ${x.catalizador || "—"} · Precio objetivo: ${num(x.precio_objetivo, 0)} · Se invalida si: ${x.condicion_invalidacion || "—"} · Retorno esperado: ${pct(x.expectativa_retorno)} en ${x.horizonte_meses ?? "—"} meses`))) : el("div", { class: "vacio" }, "Sin tesis.")));
  v.append(panel("Nueva tesis o actualizar una", formulario([
    { k: "especie", t: "Especie", lista: listaActivos, obligatorio: true }, { k: "tesis", t: "Tesis", tipo: "textarea", obligatorio: true },
    { k: "catalizador", t: "Catalizador" }, { k: "precio_objetivo", t: "Precio objetivo", tipo: "number" },
    { k: "condicion_invalidacion", t: "Condición que la invalida" }, { k: "fecha_revision", t: "Fecha de revisión", tipo: "date" },
    { k: "expectativa_retorno", t: "Retorno esperado (0,15 = 15 %)", tipo: "number" }, { k: "horizonte_meses", t: "Horizonte (meses)", tipo: "number" },
  ], (d) => postear("/api/usuario/dim_tesis", { filas: [{ ...d, especie: (d.especie || "").split(" ")[0], cartera_id: eleccion.actual, creada: hoyIso(), actualizada: hoyIso() }] }))));
};

paginas.salud = async (v) => {
  const d = await api("/api/salud");
  v.append(el("div", {}, el("h1", {}, "Salud de datos y señales"), el("div", { class: "sub" }, "Estado de cada fuente, últimas corridas, validación de factores y tamaño de las tablas")));
  const consola = el("div", { class: "consola", hidden: true });
  const lanzar = async (accion, boton) => {
    boton.disabled = true; consola.hidden = false; consola.textContent = "Iniciando…";
    const r = await postear("/api/acciones/" + accion);
    if (!r.ok) { consola.textContent = r.problemas.join("\n"); boton.disabled = false; return; }
    const sondeo = setInterval(async () => {
      const t = await api("/api/acciones/" + r.trabajo);
      consola.textContent = t.salida.join("\n") || "Corriendo…";
      consola.scrollTop = consola.scrollHeight;
      if (t.fin) { clearInterval(sondeo); boton.disabled = false; consola.textContent += `\n\nTerminó (código ${t.codigo}).`; refrescarEstado(); }
    }, 1500);
  };
  const b1 = el("button", { type: "button", class: "boton primario" }, "Leer la Bolsa de Colombia");
  const b2 = el("button", { type: "button", class: "boton" }, "Actualizar el modelo");
  const b3 = el("button", { type: "button", class: "boton" }, "Verificar el contrato");
  b1.onclick = () => lanzar("leer_bvc", b1); b2.onclick = () => lanzar("actualizar_modelo", b2);
  const contrato = el("div");
  b3.onclick = async () => { b3.disabled = true; contrato.textContent = "Verificando…"; const r = await api("/api/salud/contrato");
    contrato.replaceChildren(r.problemas.length ? el("div", { class: "estado-mal" }, r.problemas.join("; ")) : el("div", { class: "estado-ok" }, "Contrato sin problemas")); b3.disabled = false; };
  v.append(panel("Acciones",
    el("p", { class: "desc" }, "Leer la Bolsa: después de las 4:15 p. m. de un día bursátil; se abre una ventana de Chromium (la Bolsa no permite navegadores sin ventana). Actualizar el modelo: recalcula técnico, riesgo, valoración, puntajes, calendario y carteras (unos minutos)."),
    el("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } }, b1, b2, b3), consola, contrato));
  const claseEstado = (e) => e === "al día" ? "estado-ok" : (e || "").startsWith("manual") ? "estado-medio" : "estado-mal";
  v.append(panel("Fuentes", tabla([
    { k: "fuente", t: "Fuente", izq: true }, { k: "ultimo_dato", t: "Último dato", f: (r) => fecha(r.ultimo_dato) },
    { k: "rezago_dias_habiles", t: "Rezago (días hábiles)" }, { k: "estado", t: "Estado", izq: true, f: (r) => el("span", { class: claseEstado(r.estado) }, r.estado) },
    { k: "estado_ultima_ejecucion", t: "Última corrida", izq: true },
  ], d.fuentes)));
  v.append(el("div", { class: "fila" },
    panel("Validación de factores", tabla([
      { k: "factor", t: "Factor", izq: true }, { k: "veredicto", t: "Veredicto", izq: true },
      { k: "ic_medio", t: "IC", f: (r) => num(r.ic_medio, 3) }, { k: "ic_t", t: "t", f: (r) => num(r.ic_t, 1) },
      { k: "ic_medio_fuera_muestra", t: "IC desde 2018", f: (r) => num(r.ic_medio_fuera_muestra, 3) },
      { k: "exceso_superior_neto", t: "Exceso neto", f: (r) => pct(r.exceso_superior_neto, 2) },
    ], d.validacion)),
    panel("Tablas del modelo", tabla([{ k: "tabla", t: "Tabla", izq: true }, { k: "capa", t: "Capa", izq: true }, { k: "megabytes", t: "MB", f: (r) => num(r.megabytes, 1) }, { k: "pagina", t: "Página", izq: true }], d.tablas, { orden: "megabytes" }))));
  v.append(panel("Últimas corridas", tabla([
    { k: "ts_utc", t: "Hora (UTC)", izq: true, f: (r) => (r.ts_utc || "").replace("T", " ").slice(0, 16) }, { k: "fuente_id", t: "Fuente", izq: true },
    { k: "estado", t: "Estado", izq: true }, { k: "filas_validas", t: "Filas" }, { k: "detalle", t: "Detalle", izq: true, f: (r) => (r.detalle || "").slice(0, 120) },
  ], d.ejecuciones)));
};

paginas.aprender = async (v, id) => {
  v.append(el("div", {}, el("h1", {}, "Aprender"), el("div", { class: "sub" }, "Qué es, qué hace y para qué sirve cada variable del sistema. Los textos marcados como borrador están pendientes de revisión.")));
  const porCategoria = {};
  for (const x of Object.values(estado.variables)) (porCategoria[x.categoria] ||= []).push(x);
  for (const [cat, lista] of Object.entries(porCategoria).sort()) {
    v.append(panel(cat.replace("_", " "), el("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: "12px" } },
      lista.map((x) => el("article", { id: "var-" + x.id, class: "panel", style: { background: "var(--panel2)", outline: x.id === id ? "2px solid var(--acento)" : null } },
        el("div", { class: "encabezado" }, el("strong", {}, x.nombre), el("span", { class: "chip gris" }, x.revisado ? x.nivel : x.nivel + " · borrador")),
        el("p", { style: { margin: 0, fontSize: "13px" } }, x.que_es),
        el("p", { style: { margin: 0, fontSize: "12.5px", color: "var(--texto2)" } }, el("strong", {}, "Para qué: "), x.para_que_sirve),
        el("p", { style: { margin: 0, fontSize: "12.5px", color: "var(--texto2)" } }, el("strong", {}, "Cómo leerla: "), x.como_leer),
        el("p", { class: "nota", style: { margin: 0 } }, "Fórmula: " + x.formula),
        x.relacionadas ? el("p", { class: "nota", style: { margin: 0 } }, "Relacionadas: ", x.relacionadas.split(", ").map((r, i) => [i ? ", " : "", el("a", { href: "#/aprender/" + r, style: { color: "var(--acento)" } }, estado.variables[r]?.nombre || r)])) : null)))));
  }
  if (id) requestAnimationFrame(() => document.getElementById("var-" + id)?.scrollIntoView({ block: "center" }));
};

// ------------------------------------------------------------------ navegación
async function refrescarEstado() {
  estado.info = await api("/api/estado");
  $("#pie-lateral").replaceChildren("Datos al " + fecha(estado.info.ultimo_precio), el("br"), "Puntajes al " + fecha(estado.info.fecha_matriz), el("br"),
    `${estado.info.carteras.length} carteras · ${estado.info.perfiles.length} perfiles`);
}

async function ir() {
  const [ruta, parametro] = (location.hash.replace(/^#\/?/, "") || "mercado").split("/");
  const nombre = paginas[ruta] ? ruta : "mercado";
  document.querySelectorAll("#menu a").forEach((a) => { if (a.dataset.ruta !== nombre) a.removeAttribute("aria-current"); else a.setAttribute("aria-current", "page"); });
  const vista = $("#vista");
  vista.replaceChildren(cargando());
  ocultarTip(); cerrarPop();
  const nueva = el("div", { style: { display: "flex", flexDirection: "column", gap: "16px" } });
  try {
    await paginas[nombre](nueva, parametro ? decodeURIComponent(parametro) : null);
    vista.replaceChildren(nueva);
  } catch (e) {
    vista.replaceChildren(panel("Error", el("p", { class: "mensaje error" }, String(e.message || e)),
      el("p", { class: "desc" }, "Si falta una tabla, corra invest modelo (o el botón Actualizar el modelo en Salud de datos).")));
  }
}

async function arrancar() {
  const aprendizaje = localStorage.getItem("aprendizaje") !== "no";
  const boton = $("#modo-aprendizaje");
  const aplicar = (on) => { document.body.classList.toggle("sin-aprendizaje", !on); boton.setAttribute("aria-checked", String(on)); };
  aplicar(aprendizaje);
  boton.addEventListener("click", () => { const on = boton.getAttribute("aria-checked") !== "true"; localStorage.setItem("aprendizaje", on ? "si" : "no"); aplicar(on); });
  try {
    const [vars, activos] = await Promise.all([api("/api/variables"), api("/api/activos"), refrescarEstado()]);
    estado.variables = Object.fromEntries(vars.map((x) => [x.id, x]));
    estado.activos = activos;
    $("#lista-activos").replaceChildren(...activos.map((a) => el("option", { value: `${a.especie} ${a.nemotecnico || ""} ${a.nombre_corto || ""}`.trim() })));
  } catch (e) { $("#pie-lateral").textContent = "Sin conexión con el modelo: " + e.message; }
  const buscar = $("#buscar");
  buscar.addEventListener("change", () => {
    const t = buscar.value.trim().toLowerCase(); if (!t) return;
    const a = estado.activos.find((x) => t.startsWith(x.especie)) || estado.activos.find((x) => (x.nemotecnico || "").toLowerCase() === t)
      || estado.activos.find((x) => `${x.nemotecnico || ""} ${x.nombre_corto || ""} ${x.nombre || ""}`.toLowerCase().includes(t));
    if (a) { location.hash = "#/ficha/" + a.especie; buscar.value = ""; }
  });
  $("#nota-datos").textContent = "Herramienta educativa: no son recomendaciones de compra o venta.";
  addEventListener("hashchange", ir);
  let espera; addEventListener("resize", () => { clearTimeout(espera); espera = setTimeout(ir, 300); });
  ir();
}
arrancar();
