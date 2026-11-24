"""Splits, reagrupaciones y cambios de valor nominal: detección, revisión y ajuste.

La SFC publica los precios sin ajustar: el día de un split 1:10 el precio cae 90 % y el día de una
reagrupación 500:1 se multiplica por 500. Este módulo:

1. **Detecta** los días con un salto mayor que ``datos.salto_atipico_accion`` (50 %).
2. **Clasifica** cada salto:
   - ``error_puntual``: el precio vuelve al nivel anterior en el siguiente día negociado (el día del
     regreso también cuenta). Es un error de la fuente, no un split; no se ajusta y sigue marcado
     como salto atípico.
   - ``split_con_evento``: la razón de precios está cerca de un factor redondo (2, 10, 100, 500…),
     el nuevo nivel se sostiene y hay un evento del emisor en la información relevante de la SFC
     alrededor de la fecha (capital, valor nominal, split, fusión o escisión). Se aplica solo.
   - ``split_sin_evento``: factor redondo y nivel sostenido, sin evento encontrado (p. ej. antes
     de 2007, cuando empieza el histórico de eventos). Se aplica solo si se aprueba.
   - ``sin_explicacion``: el salto se sostiene pero no se parece a un factor redondo.
3. Escribe ``invest-data/revision/splits.csv`` (punto y coma, abre en Excel) con todos menos los
   errores puntuales. En ``revision``: ``aprobado``, ``corregido`` (con ``factor_revisado``),
   ``rechazado``, ``mercado`` o ``atipico``. Lo marcado no se sobrescribe.
   - ``rechazado``: no fue un split; el día sigue marcado como salto atípico (p. ej. un precio de
     referencia sin negociación).
   - ``mercado``: no fue un split pero el salto es real (OPA, quiebra, reanudación tras una
     suspensión). No se ajusta y deja de marcarse como atípico, para que entre en volatilidad y
     backtests.
   - ``atipico``: no se puede saber si fue un split (antes de 2007, sin avisos de la SFC) o el salto
     junta meses de movimiento porque la acción no tuvo negocios; no se ajusta y sigue marcado como
     atípico, fuera de volatilidad y backtests.
4. Deja ``gold/fact_split`` con los que se aplican: los ``split_con_evento`` no rechazados y los
   aprobados o corregidos. ``retornos.construir`` los usa para que el retorno de ese día sea el
   de mercado. Los marcados ``mercado`` quedan en ``gold/fact_salto_mercado``.

Convención del factor: acciones nuevas por cada acción anterior. Split 1:10 -> 10 (el precio se
divide por 10). Reagrupación 500:1 -> 0,002 (el precio se multiplica por 500).
"""

from __future__ import annotations

import csv
import math
import re
from datetime import timedelta

import polars as pl

from invest import eventos, storage
from invest.config import convenciones, data_dir

FACTORES_REDONDOS = [2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100, 200, 250, 500, 1000]
# El factor observado puede diferir del redondo hasta ~12 % por el movimiento del mercado ese día.
# Con más tolerancia, saltos de 75-120 % de acciones poco líquidas parecen splits 1:2 sin serlo.
TOLERANCIA_LOG = 0.12
REVERSION = 0.5            # si el siguiente precio está a menos de 50 % del anterior al salto, volvió
DIAS_EVENTO_ANTES, DIAS_EVENTO_DESPUES = 120, 15
# Solo eventos que hablan de cambiar el número de acciones. "Capital" a secas no sirve: aparece en
# guías de inversión de capital, aumentos de capital autorizado, etc.
PATRON_EVENTO = (r"\bSPLIT\b|DESDOBLAMIENTO|FRACCIONAMIENTO|REAGRUPA|CONSOLIDACION DE (LAS )?ACCIONES|"
                 r"VALOR NOMINAL|\bFUSION\b|\bESCISION\b")
ARCHIVO = "splits.csv"
COLUMNAS = [
    "especie", "emisor_id", "nombre", "fecha_anterior", "precio_anterior", "fecha", "precio",
    "precio_siguiente", "razon_precios", "factor_propuesto", "clasificacion", "eventos", "resumen_evento",
    "revision", "factor_revisado", "notas_revision",
]


def factor_redondo(razon_precios: float) -> float | None:
    """Factor de acciones (nuevas por anterior) si la razón de precios es casi un factor redondo."""
    if razon_precios <= 0:
        return None
    objetivo = 1 / razon_precios  # caída de 90 % -> razón 0,1 -> factor 10
    candidatos = FACTORES_REDONDOS + [1 / k for k in FACTORES_REDONDOS]
    mejor = min(candidatos, key=lambda k: abs(math.log(objetivo / k)))
    return mejor if abs(math.log(objetivo / mejor)) <= TOLERANCIA_LOG else None


def _formato_factor(f: float | None) -> str:
    if f is None:
        return ""
    return f"{f:g}" if f >= 1 else f"{f:.6g}".replace(".", ",")


def _fecha_iso(texto: str) -> str:
    """Excel guarda las fechas como dd/mm/aaaa al editar el CSV; se normalizan a AAAA-MM-DD."""
    t = (texto or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else t[:10]


def _numero(texto: str) -> float | None:
    t = (texto or "").strip().replace(" ", "")
    if not t:
        return None
    t = re.sub(r"^(\d+):(\d+):00$", r"\1:\2", t)  # Excel convierte "1:10" en la hora "1:10:00"
    if ":" in t:  # "1:10" -> 10 ; "500:1" -> 0,002
        a, b = t.split(":", 1)
        try:
            return float(b.replace(",", ".")) / float(a.replace(",", "."))
        except (ValueError, ZeroDivisionError):
            return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def detectar() -> pl.DataFrame:
    """Saltos atípicos con su clasificación y el evento que los respalda (si lo hay)."""
    precios = storage.leer_tabla("silver", "fact_precio_sfc")
    vacio = pl.DataFrame(schema={c: pl.Utf8 for c in COLUMNAS})
    if precios is None or precios.is_empty():
        return vacio
    umbral = float(convenciones()["datos"].get("salto_atipico_accion", 0.5))
    p = precios.select("especie", "fecha", "precio_promedio").sort(["especie", "fecha"]).with_columns(
        pl.col("precio_promedio").shift(1).over("especie").alias("previo"),
        pl.col("fecha").shift(1).over("especie").alias("fecha_previa"),
        pl.col("precio_promedio").shift(-1).over("especie").alias("siguiente"),
        pl.col("precio_promedio").shift(2).over("especie").alias("previo2"),
    )
    saltos = p.filter((pl.col("precio_promedio") / pl.col("previo") - 1).abs() > umbral)
    if saltos.is_empty():
        return vacio

    activos = storage.leer_dim("dim_activo")
    info = {} if activos is None else {
        r["especie"]: r for r in activos.select("especie", "emisor_id", "nombre").iter_rows(named=True)}
    ev = storage.leer_tabla("gold", "fact_evento_corporativo")
    if ev is not None and not ev.is_empty():
        ev = ev.filter(pl.col("emisor_id").is_not_null()).with_columns(
            (pl.col("tema") + " " + pl.col("resumen")).map_elements(eventos.normal, return_dtype=pl.Utf8).alias("_t"))
        ev = ev.filter(pl.col("_t").str.contains(PATRON_EVENTO))

    filas = []
    for s in saltos.iter_rows(named=True):
        razon = s["precio_promedio"] / s["previo"]
        volvio = s["siguiente"] is not None and abs(s["siguiente"] / s["previo"] - 1) < REVERSION
        # El día en que el precio regresa después de un error puntual también es parte del error
        regreso = (s["previo2"] is not None and abs(razon - 1) > umbral
                   and abs(s["precio_promedio"] / s["previo2"] - 1) < REVERSION)
        volvio = volvio or regreso
        factor = factor_redondo(razon)
        meta = info.get(s["especie"], {})
        emisor = meta.get("emisor_id")
        relacionados = pl.DataFrame()
        if ev is not None and emisor:
            relacionados = ev.filter(
                (pl.col("emisor_id") == emisor)
                & pl.col("fecha").is_between(s["fecha"] - timedelta(days=DIAS_EVENTO_ANTES),
                                             s["fecha"] + timedelta(days=DIAS_EVENTO_DESPUES))
            ).sort("fecha", descending=True)
        if volvio:
            clase = "error_puntual"
        elif factor is None:
            clase = "sin_explicacion"
        elif not relacionados.is_empty():
            clase = "split_con_evento"
        else:
            clase = "split_sin_evento"
        filas.append({
            "especie": s["especie"], "emisor_id": emisor or "", "nombre": meta.get("nombre") or "",
            "fecha_anterior": s["fecha_previa"].isoformat(), "precio_anterior": _decimal(s["previo"]),
            "fecha": s["fecha"].isoformat(), "precio": _decimal(s["precio_promedio"]),
            "precio_siguiente": _decimal(s["siguiente"]), "razon_precios": _decimal(razon),
            "factor_propuesto": _formato_factor(factor), "clasificacion": clase,
            "eventos": ",".join(relacionados.get_column("evento_id").to_list()[:5]) if not relacionados.is_empty() else "",
            "resumen_evento": (relacionados.get_column("resumen")[0][:300] if not relacionados.is_empty() else ""),
            "revision": "", "factor_revisado": "", "notas_revision": "",
        })
    return pl.DataFrame(filas, schema={c: pl.Utf8 for c in COLUMNAS})


def _decimal(v) -> str:
    return "" if v is None else f"{float(v):.6g}".replace(".", ",")


def ruta_revision():
    carpeta = data_dir() / "revision"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / ARCHIVO


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
        pl.col("fecha").map_elements(_fecha_iso, return_dtype=pl.Utf8),
        pl.col("fecha_anterior").map_elements(_fecha_iso, return_dtype=pl.Utf8),
    )


def construir() -> tuple[pl.DataFrame | None, dict]:
    """Detecta, actualiza revision/splits.csv sin pisar lo revisado y escribe gold/fact_split."""
    detectados = detectar()
    resumen = {k: detectados.filter(pl.col("clasificacion") == k).height
               for k in ("split_con_evento", "split_sin_evento", "sin_explicacion", "error_puntual")}
    para_revisar = detectados.filter(pl.col("clasificacion") != "error_puntual")
    previa = leer_revision()
    if previa is not None:
        decididas = previa.filter(pl.col("revision") != "")
        para_revisar = pl.concat([decididas, para_revisar.join(decididas, on=["especie", "fecha"], how="anti")])
    if para_revisar.is_empty() and previa is None:
        return None, resumen
    with open(ruta_revision(), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNAS)
        w.writerows(para_revisar.sort(["clasificacion", "especie", "fecha"]).select(COLUMNAS).rows())

    aplicar, de_mercado = [], []
    for r in para_revisar.iter_rows(named=True):
        if r["revision"] == "rechazado":
            continue
        if r["revision"] == "mercado":
            de_mercado.append({"especie": r["especie"], "fecha": r["fecha"], "notas": r["notas_revision"]})
            continue
        if r["revision"] == "corregido":
            factor = _numero(r["factor_revisado"])
        elif r["revision"] == "aprobado" or (r["revision"] == "" and r["clasificacion"] == "split_con_evento"):
            factor = _numero(r["factor_propuesto"])
        else:
            continue
        if factor and factor > 0:
            aplicar.append({"especie": r["especie"], "fecha": r["fecha"], "factor_acciones": factor,
                            "origen": "revisado" if r["revision"] else "automatico",
                            "clasificacion": r["clasificacion"]})
    gold = pl.DataFrame(aplicar, schema={"especie": pl.Utf8, "fecha": pl.Utf8, "factor_acciones": pl.Float64,
                                         "origen": pl.Utf8, "clasificacion": pl.Utf8}
                        ).with_columns(pl.col("fecha").str.to_date())
    storage.escribir_completa("gold", "fact_split", gold)
    storage.escribir_completa("gold", "fact_salto_mercado", pl.DataFrame(
        de_mercado, schema={"especie": pl.Utf8, "fecha": pl.Utf8, "notas": pl.Utf8}
    ).with_columns(pl.col("fecha").str.to_date()))
    resumen["aplicados"] = gold.height
    resumen["mercado"] = len(de_mercado)
    resumen["atipico"] = para_revisar.filter(pl.col("revision") == "atipico").height
    resumen["por_revisar"] = para_revisar.filter(
        (pl.col("revision") == "") & (pl.col("clasificacion") != "split_con_evento")).height
    return gold, resumen
