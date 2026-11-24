"""Dividendos en efectivo desde historiales públicos (Investing.com y stockanalysis.com).

Sirve mientras no se procesen los PDF de la SFC con los dos agentes (``dividendos.py``), y después
como contraste. Flujo de ``construir()``:

1. **Carga** los historiales copiados en ``invest-data/fuentes_web/dividendos/<fuente>/`` según
   ``config/dividendos_web.yaml``. Investing tiene el historial completo; stockanalysis los últimos
   20 pagos. Ambas publican los valores ajustados por splits, así que se llevan al valor de la época
   con los splits de ``gold/fact_split`` (acciones nuevas por anterior, de los splits posteriores a
   la fecha ex-dividendo).
2. **Une** las dos fuentes y quita duplicados: el mismo valor en la misma acción a pocos días es el
   mismo pago (Investing repite algunos). Se queda con la fecha que confirma stockanalysis o, si no,
   con la que cuadra con la regla del periodo ex-dividendo.
3. **Verifica** cada pago con lo que no viene de esas páginas:
   - la regla de la BVC: la fecha ex-dividendo debe estar a los días hábiles de la época antes del pago;
   - el precio de la SFC: el pago no puede pasar de ``rendimiento_maximo`` del precio anterior ni ser
     muchas veces mayor o menor que los demás pagos del año (así se detectan escisiones, repartos en
     acciones y valores mal ajustados por splits);
   - la información relevante de la SFC: se anota si hay un aviso de dividendos del emisor antes de
     la fecha y si sus montos coinciden (informativo: los avisos se clasifican por palabras clave y no
     se captan todos).
   Un pago que estaba en el periodo cubierto por stockanalysis y no aparece allí también se revisa.
4. ``verificado`` si pasa todo; ``revisar`` con los motivos si no.
5. **Revisión humana** en ``invest-data/revision/dividendos_web.csv``: ``aprobado``, ``corregido``
   (con ``valor_revisado``) o ``rechazado``. Lo marcado no se sobrescribe.
6. **gold/fact_dividendo_web**: lo verificado sin rechazar y lo aprobado o corregido. ``retornos``
   lo usa cuando no hay dividendo de los PDF para esa acción y fecha.
"""

from __future__ import annotations

import csv
import re
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import yaml

from invest import storage
from invest import sucesiones
from invest.calendario import dia_habil_siguiente_o_mismo, es_habil
from invest.config import CONFIG_DIR, data_dir

ARCHIVO_REVISION = "dividendos_web.csv"
COLUMNAS = [
    "especie", "nombre", "fecha_ex", "fecha_pago", "valor", "moneda", "valor_publicado", "factor_split",
    "tipo_fuente", "fuentes", "regla_ex", "rendimiento", "aviso_sfc", "monto_sfc", "estado", "motivos",
    "revision", "valor_revisado", "notas_revision",
]
REVISIONES_VALIDAS = {"", "aprobado", "corregido", "rechazado"}
_RE_MONTO = re.compile(r"\$\s?(\d[\d\.,]*)")


def config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "dividendos_web.yaml").read_text(encoding="utf-8"))


def _fecha(t: str) -> date | None:
    t = (t or "").strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            pass
    return None


def _num(t) -> float | None:
    if t is None:
        return None
    s = str(t).strip().replace("$", "").replace(" ", "")
    if not s:
        return None
    if "," in s:  # coma decimal (como la escribe Excel en español)
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _decimal(v: float | None) -> str:
    return "" if v is None else (f"{v:.6f}".rstrip("0").rstrip(".")).replace(".", ",")


def monto_texto(t: str) -> float | None:
    """'$1.250,50' -> 1250.5; '$107' -> 107; '$2.300' -> 2300."""
    t = t.rstrip(".,")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        ent, dec = t.rsplit(",", 1)
        t = (ent.replace(",", "") + "." + dec) if len(dec) <= 2 else t.replace(",", "")
    elif "." in t:
        ent, dec = t.rsplit(".", 1)
        t = t.replace(".", "") if len(dec) == 3 else t
    try:
        return float(t)
    except ValueError:
        return None


# --------------------------------------------------------------------------- carga
def leer_fuente(ruta: Path, fuente: str) -> list[dict]:
    """Líneas 'fecha_ex;valor;tipo;fecha_pago[;moneda]' (investing) o 'fecha_ex;valor;fecha_pago' (stockanalysis).

    El quinto campo, opcional, marca la moneda de esa fila (``USD``). Hace falta para Mineros: Investing
    mezcla pagos en dólares con pagos convertidos a pesos en la misma tabla.
    """
    if not ruta.exists():
        return []
    filas = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        partes = [p.strip() for p in linea.split(";")]
        if len(partes) < 3 or not _fecha(partes[0]):
            continue
        moneda = None
        if fuente == "investing":
            tipo, pago = (partes[2], partes[3] if len(partes) > 3 else "")
            moneda = partes[4].upper() if len(partes) > 4 and partes[4] else None
        else:
            tipo, pago = "", partes[2]
        valor = float(partes[1]) if partes[1] else None
        if valor is None or valor <= 0:
            continue
        filas.append({"fecha_ex": _fecha(partes[0]), "valor_publicado": valor, "tipo_fuente": tipo,
                      "fecha_pago": _fecha(pago), "fuente": fuente, "moneda": moneda})
    return filas


def factor_posterior(splits: list[tuple[date, float]], fecha_ex: date) -> float:
    """Producto de los factores (acciones nuevas por anterior) de los splits después de ``fecha_ex``."""
    f = 1.0
    for dia, factor in splits:
        if dia > fecha_ex:
            f *= factor
    return f


def _dias_habiles(a: date, b: date) -> int:
    """Días hábiles de a hasta b (positivo si b es posterior)."""
    if a == b:
        return 0
    signo, x, y = (1, a, b) if a < b else (-1, b, a)
    n, d = 0, x
    while d < y:
        d += timedelta(days=1)
        if es_habil(d):
            n += 1
    return signo * n


def _regla_ex(fecha_ex: date, fecha_pago: date | None) -> int | None:
    """Diferencia en días hábiles entre la fecha ex publicada y la que da la regla de la BVC.

    Se prueba con los periodos de ``convenciones.yaml`` (10 días hábiles hasta 2012, 4 después) y se
    devuelve la diferencia más pequeña: la fecha exacta del cambio de regla no se conoce. Una fecha de
    pago igual o anterior a la ex-dividendo es un error de la fuente y no se usa.
    """
    if fecha_pago is None or fecha_pago <= fecha_ex:
        return None
    from invest.calendario import restar_dias_habiles
    from invest.config import convenciones
    pago = dia_habil_siguiente_o_mismo(fecha_pago)
    ex = dia_habil_siguiente_o_mismo(fecha_ex)
    periodos = {int(r["dias_habiles"]) for r in convenciones()["dividendos"]["periodo_ex_dividendo"]}
    return min((_dias_habiles(restar_dias_habiles(pago, n), ex) for n in periodos), key=abs)


# --------------------------------------------------------------------------- verificación
def _avisos(emisor_id: str | None, eventos: pl.DataFrame | None) -> list[tuple[date, list[float]]]:
    if emisor_id is None or eventos is None:
        return []
    ev = eventos.filter((pl.col("emisor_id") == emisor_id) & pl.col("categoria").is_in(["dividendo", "ex_dividendo"]))
    return [(f, [m for m in (monto_texto(x) for x in _RE_MONTO.findall(r or "")) if m])
            for f, r in ev.select("fecha", "resumen").iter_rows()]


def _monto_coincide(valor: float, montos: list[float], tol: float) -> bool:
    for m in montos:
        for k in (1, 2, 3, 4, 6, 12):
            if abs(valor * k - m) <= tol * m:
                return True
    return False


def _unir(filas: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """Une investing y stockanalysis y quita los duplicados (mismo valor a pocos días)."""
    filas = sorted(filas, key=lambda r: r["fecha_ex"])
    tol, dias = cfg["tolerancia_valor"], cfg["dias_duplicado"]
    grupos: list[list[dict]] = []
    for r in filas:
        for g in grupos[::-1][:6]:
            if any(abs((r["fecha_ex"] - x["fecha_ex"]).days) <= dias
                   and abs(r["valor"] - x["valor"]) <= tol * max(x["valor"], 1e-9) for x in g):
                g.append(r)
                break
        else:
            grupos.append([r])
    salida, descartados = [], 0
    for g in grupos:
        sa = [x for x in g if x["fuente"] == "stockanalysis"]
        fechas_sa = {x["fecha_ex"] for x in sa}
        candidatos = [x for x in g if x["fuente"] == "investing"] or g
        ok = lambda x: x["fecha_ex"] in fechas_sa or (x["regla_ex"] is not None and abs(x["regla_ex"]) <= 1)
        elegido = next((x for x in candidatos if x["fecha_ex"] in fechas_sa), None) \
            or next((x for x in candidatos if ok(x)), None) or candidatos[0]
        elegido = dict(elegido)
        elegido["fuentes"] = "+".join(sorted({x["fuente"] for x in g}))
        elegido["confirmado_sa"] = bool(sa)
        descartados += max(len([x for x in g if x["fuente"] == "investing"]) - 1, 0)
        salida.append(elegido)
    return salida, descartados


def _trm_tabla() -> pl.DataFrame:
    fx = storage.leer_tabla("silver", "fact_fx_diaria")
    if fx is None:
        return pl.DataFrame(schema={"fecha": pl.Date, "tasa": pl.Float64})
    return fx.filter((pl.col("moneda_origen") == "USD") & (pl.col("moneda_destino") == "COP")).select("fecha", "tasa")


def _rendimiento(px: pl.DataFrame | None, r: dict) -> float | None:
    if px is None or px.is_empty():
        return None
    previos = px.filter(pl.col("fecha") < r["fecha_ex"])
    if previos.is_empty():
        return None
    precio = previos.get_column("precio_promedio")[-1]
    a_cop = r.get("a_cop", 1.0)  # TRM del día ex para pagos en dólares; None si no hay tasa
    return r["valor"] * a_cop / precio if precio and a_cop else None


def detectar() -> tuple[pl.DataFrame, dict]:
    cfg = config()
    base = data_dir()
    desde = date.fromisoformat(str(cfg["desde"]))
    activos = storage.leer_dim("dim_activo")
    nombres = {} if activos is None else dict(activos.select("especie", "nombre").iter_rows())
    emisores = {} if activos is None else dict(activos.select("especie", "emisor_id").iter_rows())
    splits_tabla = storage.leer_tabla("gold", "fact_split")
    precios = sucesiones.precios_sfc()
    eventos = storage.leer_tabla("gold", "fact_evento_corporativo")
    resumen = {"activos": 0, "pagos": 0, "duplicados": 0, "verificado": 0, "revisar": 0}
    salida = []
    tasas = None
    for a in cfg["activos"]:
        especie = a["especie"]
        sp = [] if splits_tabla is None else sorted(
            splits_tabla.filter(pl.col("especie") == especie).select("fecha", "factor_acciones").iter_rows())
        filas = []
        for fuente, opciones in cfg["fuentes"].items():
            clave = a.get(fuente)
            if not clave:
                continue
            for r in leer_fuente(base / opciones["carpeta"] / f"{clave}.txt", fuente):
                if r["fecha_ex"] < desde:
                    continue
                factor = factor_posterior(sp, r["fecha_ex"]) if opciones.get("ajustado_por_splits") else 1.0
                r.update(valor=r["valor_publicado"] * factor, factor_split=factor,
                         regla_ex=_regla_ex(r["fecha_ex"], r["fecha_pago"]))
                filas.append(r)
        if not filas:
            continue
        resumen["activos"] += 1
        unidas, dup = _unir(filas, cfg)
        resumen["duplicados"] += dup
        fechas_sa = [r["fecha_ex"] for r in filas if r["fuente"] == "stockanalysis"]
        ventana_sa = (min(fechas_sa), max(fechas_sa)) if fechas_sa else None
        px = None if precios is None else precios.filter(pl.col("especie") == especie).select(
            "fecha", "precio_promedio").sort("fecha")
        moneda_activo = (a.get("moneda") or "COP").upper()
        for r in unidas:
            r["moneda"] = (r.get("moneda") or moneda_activo).upper()
            if r["moneda"] != "COP":
                # Pago en dólares (Mineros): el rendimiento se mide en pesos con la TRM del día ex
                tasas = tasas if tasas is not None else dict(_trm_tabla().iter_rows())
                r["a_cop"] = tasas.get(r["fecha_ex"]) if r["moneda"] == "USD" else None
        avisos = _avisos(emisores.get(especie), eventos)
        for r in unidas:
            motivos = []
            for ex in a.get("excluir", []) or []:
                if date.fromisoformat(str(ex["desde"])) <= r["fecha_ex"] <= date.fromisoformat(str(ex["hasta"])):
                    motivos.append(ex["motivo"])
            if ventana_sa and ventana_sa[0] <= r["fecha_ex"] <= ventana_sa[1] and not r["confirmado_sa"]:
                motivos.append("no aparece en stockanalysis (duplicado o reparto que no es en efectivo)")
            if not r["confirmado_sa"] and r["regla_ex"] is not None and abs(r["regla_ex"]) > 1:
                motivos.append(f"la fecha ex no cuadra con la fecha de pago ({r['regla_ex']:+d} días hábiles)")
            rend = _rendimiento(px, r)
            if rend is not None and rend > cfg["rendimiento_maximo"]:
                motivos.append(f"el pago es {rend:.0%} del precio anterior")
            vecinos = [x for x in unidas if x is not r and abs((x["fecha_ex"] - r["fecha_ex"]).days) <= 400]
            # Se compara en rendimiento (pago / precio) para que un split en medio no cuente como salto
            propios = _rendimiento(px, r) or r["valor"]
            otros = [(_rendimiento(px, x) if _rendimiento(px, r) else x["valor"]) for x in vecinos]
            otros = [o for o in otros if o]
            if len(otros) >= 2:
                med = statistics.median(otros)
                if med > 0 and (propios / med > cfg["veces_mediana"] or med / propios > cfg["veces_mediana"]):
                    motivos.append(f"{propios / med:.1f} veces la mediana de los pagos cercanos")
            cercanos = [m for f, m in avisos
                        if r["fecha_ex"] - timedelta(days=cfg["dias_evento_antes"]) <= f
                        <= r["fecha_ex"] + timedelta(days=cfg["dias_evento_despues"])]
            # Informativo: la categoría de los avisos se asigna por palabras clave y no los capta todos
            hay_aviso = bool(cercanos)
            coincide = _monto_coincide(r["valor"], [m for ms in cercanos for m in ms], cfg["tolerancia_valor"])
            estado = "revisar" if motivos else "verificado"
            resumen["pagos"] += 1
            resumen[estado] += 1
            salida.append({
                "especie": especie, "nombre": nombres.get(especie, ""), "fecha_ex": r["fecha_ex"].isoformat(),
                "fecha_pago": r["fecha_pago"].isoformat() if r["fecha_pago"] else "", "valor": _decimal(r["valor"]),
                "moneda": r["moneda"], "valor_publicado": _decimal(r["valor_publicado"]), "factor_split": _decimal(r["factor_split"]),
                "tipo_fuente": r["tipo_fuente"], "fuentes": r["fuentes"],
                "regla_ex": "" if r["regla_ex"] is None else str(r["regla_ex"]),
                "rendimiento": "" if rend is None else _decimal(round(rend, 5)),
                "aviso_sfc": "sí" if hay_aviso else "no", "monto_sfc": "coincide" if coincide else "",
                "estado": estado, "motivos": " | ".join(motivos), "revision": "", "valor_revisado": "",
                "notas_revision": "",
            })
    return pl.DataFrame(salida, schema={c: pl.Utf8 for c in COLUMNAS}), resumen


# --------------------------------------------------------------------------- revisión y gold
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
    cab = [c.strip() for c in filas[0]]
    datos = [dict(zip(cab, f + [""] * (len(cab) - len(f)))) for f in filas[1:] if any(f)]
    df = pl.DataFrame(datos, schema={c: pl.Utf8 for c in cab}).fill_null("")
    for c in COLUMNAS:
        if c not in df.columns:
            df = df.with_columns(pl.lit("").alias(c))
    return df.select(COLUMNAS).with_columns(
        pl.col("revision").str.strip_chars().str.to_lowercase(),
        # Excel guarda las fechas como dd/mm/aaaa
        pl.col("fecha_ex").map_elements(lambda t: _fecha(t).isoformat() if _fecha(t) else t, return_dtype=pl.Utf8),
    )


def construir() -> tuple[pl.DataFrame | None, dict]:
    """Detecta y verifica, actualiza revision/dividendos_web.csv sin pisar lo revisado y escribe gold."""
    detectados, resumen = detectar()
    previa = leer_revision()
    tabla = detectados
    if previa is not None:
        decididas = previa.filter(pl.col("revision") != "")
        tabla = pl.concat([decididas, detectados.join(decididas, on=["especie", "fecha_ex"], how="anti")])
    if tabla.is_empty():
        return None, resumen
    with open(ruta_revision(), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNAS)
        w.writerows(tabla.sort(["especie", "fecha_ex"]).select(COLUMNAS).rows())
    gold = []
    for r in tabla.iter_rows(named=True):
        if r["revision"] == "rechazado":
            continue
        if r["revision"] == "corregido":
            valor = _num(r["valor_revisado"])
        elif r["revision"] == "aprobado" or (r["revision"] == "" and r["estado"] == "verificado"):
            valor = _num(r["valor"])
        else:
            continue
        fecha_ex = _fecha(r["fecha_ex"])
        if valor and valor > 0 and fecha_ex:
            gold.append({"especie": r["especie"], "fecha_ex": fecha_ex, "fecha_pago": _fecha(r["fecha_pago"]),
                         "dividendo": valor, "moneda": r["moneda"] or "COP", "revisado": r["revision"] != "",
                         "fuentes": r["fuentes"]})
    gold_df = pl.DataFrame(gold, schema={"especie": pl.Utf8, "fecha_ex": pl.Date, "fecha_pago": pl.Date,
                                         "dividendo": pl.Float64, "moneda": pl.Utf8, "revisado": pl.Boolean,
                                         "fuentes": pl.Utf8})
    storage.escribir_completa("gold", "fact_dividendo_web", gold_df)
    resumen["aplicables"] = gold_df.height
    resumen["revisados_a_mano"] = tabla.filter(pl.col("revision") != "").height
    return gold_df, resumen
