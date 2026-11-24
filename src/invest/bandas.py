"""Bandas de valoración: P/VL y P/U de cada acción frente a su propia historia, sin mirar al futuro.

Inspirado en la gráfica clásica de P/VL con su media y ±1 y ±2 desviaciones estándar en ventanas de
1, 3, 5 y 10 años. Es descriptivo: dice si una acción está por encima o por debajo de su media
histórica; no es una señal (config/bandas.yaml).

- ``gold/fact_valoracion_diaria``: especie × día con negocios (los de ``gold/fact_precio_diario``):
  precio, valor patrimonial por acción y utilidad por acción de 12 meses **vigentes ese día** (as-of
  por ``fecha_disponible``, con las mismas reglas de ``metricas.fact_valoracion``: base separada, cifras
  del emisor también para las preferenciales, nulo si el corte tiene más de 15 meses, dólares a pesos
  con la TRM del día), ``p_vl`` (nulo si el valor patrimonial no es positivo) y ``p_u`` (nulo si la
  utilidad de 12 meses es ≤ 0).
- ``gold/fact_banda_valoracion_mensual``: en cada fecha de ``fact_valoracion`` (última sesión de cada
  mes y la última con precio), por especie × indicador × ventana: el valor actual (último día con
  negocios hasta esa fecha, de hace 30 días o menos), la media, la desviación estándar (muestral) y
  el percentil del actual entre los valores diarios de la ventana [fecha − N años, fecha], ``z`` =
  (actual − media) / desviación, ``n_obs``, ``fecha_desde``, ``cobertura`` (días con dato / sesiones
  XBOG de la ventana) y el último ``quiebre`` dentro de la ventana (escisión o reparto de acciones de
  config/eventos_capital.yaml, split o reagrupación de gold/fact_split y, solo para el P/VL, un salto
  de más de ``salto_vpa`` veces en el valor patrimonial por acción vigente) con un ``aviso``. Con
  cobertura menor que ``cobertura_minima`` (o menos de ``minimo_observaciones`` días) z y percentil
  quedan nulos. Con ``recortar_al_quiebre: true`` la ventana empieza en el último quiebre (la cobertura
  se mide igual sobre la ventana completa). Cada fila usa solo datos con fecha ≤ la de la fila, así que
  la serie sirve para medir después si z anticipa retornos.
- ``gold/fact_quiebre_valoracion``: todos los quiebres por especie (fecha, tipo y, si aplica solo a uno,
  el indicador), para marcarlos en la gráfica.
- ``gold/fact_banda_valoracion``: las filas de la última fecha (la foto de hoy) que usan el aplicativo
  (página Valoración y panel de la Ficha) y el screener de valoración histórica.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from invest import storage
from invest.calendario import sesiones_entre
from invest.config import cargar_yaml
from invest.metricas import fechas_valoracion, por_accion_asof

INDICADORES = ("p_vl", "p_u")
TIPOS_QUIEBRE = {"escision": "Escisión", "reparto_acciones": "Reparto de acciones",
                 "split": "Split o reagrupación", "salto_vpa": "Salto del valor patrimonial por acción"}
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

ESQUEMA_BANDA = {
    "fecha": pl.Date, "especie": pl.Utf8, "emisor_id": pl.Utf8, "indicador": pl.Utf8, "ventana": pl.Utf8,
    "anios": pl.Int32, "actual": pl.Float64, "fecha_actual": pl.Date, "media": pl.Float64,
    "desviacion": pl.Float64, "z": pl.Float64, "percentil": pl.Float64, "minimo": pl.Float64,
    "maximo": pl.Float64, "n_obs": pl.Int64, "sesiones": pl.Int64, "fecha_desde": pl.Date,
    "cobertura": pl.Float64, "suficiente": pl.Boolean, "quiebre": pl.Date, "tipo_quiebre": pl.Utf8,
    "recortada": pl.Boolean, "aviso": pl.Utf8,
}


def config() -> dict:
    return cargar_yaml("bandas.yaml")


def _fecha(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def fecha_texto(d: date) -> str:
    return f"{d.day}-{MESES[d.month - 1]}-{d.year}"


def restar_anios(d: date, anios: int) -> date:
    try:
        return d.replace(year=d.year - anios)
    except ValueError:  # 29 de febrero
        return d.replace(year=d.year - anios, day=28)


# --------------------------------------------------------------------------- quiebres
def quiebres(eventos: list[dict] | None = None, splits: pl.DataFrame | None = None) -> pl.DataFrame:
    """Eventos que parten la historia de una especie: escisiones y repartos de acciones
    (config/eventos_capital.yaml, por fecha ex) y splits o reagrupaciones aplicados (gold/fact_split)."""
    if eventos is None:
        eventos = cargar_yaml("eventos_capital.yaml").get("eventos") or []
    if splits is None:
        splits = storage.leer_tabla("gold", "fact_split")
    filas = [{"especie": e["especie"], "fecha": _fecha(e["fecha_ex"]), "tipo": e.get("tipo") or "escision"}
             for e in eventos if e.get("especie") and e.get("fecha_ex")]
    if splits is not None and not splits.is_empty():
        filas += [{"especie": r["especie"], "fecha": r["fecha"], "tipo": "split"}
                  for r in splits.select("especie", "fecha").iter_rows(named=True)]
    return (pl.DataFrame(filas, schema={"especie": pl.Utf8, "fecha": pl.Date, "tipo": pl.Utf8})
            .unique(subset=["especie", "fecha", "tipo"]).sort(["especie", "fecha"])
            .with_columns(pl.lit(None, pl.Utf8).alias("indicador")))


def saltos_vpa(diaria: pl.DataFrame, factor: float = 3.0) -> pl.DataFrame:
    """Días en que el valor patrimonial por acción vigente se multiplica o divide por más de ``factor``
    frente al día anterior: un split sin registrar, un cambio en el número de acciones o un error de la
    fuente. Parte la historia del P/VL (indicador ``p_vl``) igual que un evento de capital."""
    vacio = pl.DataFrame(schema={"especie": pl.Utf8, "fecha": pl.Date, "tipo": pl.Utf8, "indicador": pl.Utf8})
    if diaria is None or diaria.is_empty() or not factor or factor <= 1:
        return vacio
    r = (diaria.sort(["especie", "fecha"])
         .with_columns((pl.col("vpa") / pl.col("vpa").shift(1).over("especie")).alias("_r"))
         .filter((pl.col("_r") > factor) | (pl.col("_r") < 1 / factor)))
    return r.select("especie", "fecha", pl.lit("salto_vpa").alias("tipo"), pl.lit("p_vl").alias("indicador"))


# --------------------------------------------------------------------------- valoración diaria
def fact_valoracion_diaria(precios: pl.DataFrame | None = None, metricas: pl.DataFrame | None = None
                           ) -> pl.DataFrame | None:
    precios = precios if precios is not None else storage.leer_tabla("gold", "fact_precio_diario")
    metricas = metricas if metricas is not None else storage.leer_tabla("gold", "fact_metrica_fundamental")
    if precios is None or precios.is_empty():
        return None
    p = precios.filter(pl.col("precio").is_not_null() & (pl.col("precio") > 0))
    p = p.select("fecha", "especie", "emisor_id", "precio",
                 pl.col("trm") if "trm" in p.columns else pl.lit(None, pl.Float64).alias("trm"))
    if metricas is None or metricas.is_empty():
        v = p.with_columns(*[pl.lit(None, t).alias(c) for c, t in (
            ("vpa", pl.Float64), ("_corte_vpa", pl.Date), ("upa_12m", pl.Float64), ("_corte_upa_12m", pl.Date))])
    else:
        v = por_accion_asof(p, metricas, ("vpa", "upa_12m"))
    v = v.with_columns(
        pl.when(pl.col("vpa") > 0).then(pl.col("precio") / pl.col("vpa")).otherwise(None).alias("p_vl"),
        pl.when(pl.col("upa_12m") > 0).then(pl.col("precio") / pl.col("upa_12m")).otherwise(None).alias("p_u"),
    )
    return v.select("fecha", "especie", "emisor_id", "precio", "vpa", pl.col("_corte_vpa").alias("corte_vpa"),
                    "upa_12m", pl.col("_corte_upa_12m").alias("corte_upa"), "p_vl", "p_u").sort(["especie", "fecha"])


# --------------------------------------------------------------------------- bandas
def _dias(fechas) -> np.ndarray:
    return np.array(fechas, dtype="datetime64[D]").astype(np.int64)


def _de_dias(n: int) -> date:
    return date(1970, 1, 1) + timedelta(days=int(n))


def _aviso(indicador: str, tipo: str | None, quiebre: date | None, recortada: bool, suficiente: bool,
           cobertura: float | None, minima: float) -> str | None:
    partes = []
    if quiebre is not None:
        nombre = {"p_vl": "el P/VL", "p_u": "el P/U"}.get(indicador, indicador)
        motivo = ("posible split sin registrar, cambio en las acciones o error de la fuente" if tipo == "salto_vpa"
                  else "y el valor por acción publicado tarda en reflejarlo")
        texto = (f"{TIPOS_QUIEBRE.get(tipo, tipo)} el {fecha_texto(quiebre)}: {nombre} de antes y de después "
                 f"no es del todo comparable ({motivo})")
        partes.append(texto + ("; la ventana empieza ese día" if recortada else ""))
    if not suficiente:
        partes.append(f"cobertura de {round((cobertura or 0) * 100)} % (mínimo {round(minima * 100)} %): sin z")
    return "; ".join(partes) or None


def bandas_en_fechas(diaria: pl.DataFrame, fechas: list[date], quiebres_df: pl.DataFrame | None = None,
                     sesiones: list[date] | None = None, cfg: dict | None = None) -> pl.DataFrame:
    """Bandas de cada especie × indicador × ventana en cada fecha de ``fechas``, solo con los datos
    diarios de fecha ≤ la evaluada. ``diaria`` necesita ``fecha``, ``especie``, ``emisor_id`` y las
    columnas de los indicadores; ``sesiones`` son los días bursátiles (por omisión, XBOG)."""
    cfg = cfg or config()
    indicadores = [i for i in cfg.get("indicadores", INDICADORES) if i in diaria.columns]
    ventanas = {str(k): int(v) for k, v in (cfg.get("ventanas") or {"1a": 1, "3a": 3, "5a": 5, "10a": 10}).items()}
    minima = float(cfg.get("cobertura_minima", 0.6))
    min_obs = int(cfg.get("minimo_observaciones", 20))
    reciente = int(cfg.get("dias_precio_reciente", 30))
    recortar = bool(cfg.get("recortar_al_quiebre", False))
    if diaria is None or diaria.is_empty() or not fechas:
        return pl.DataFrame(schema=ESQUEMA_BANDA)
    fechas = sorted(set(fechas))
    if sesiones is None:
        sesiones = sesiones_entre(restar_anios(min(diaria.get_column("fecha").min(), fechas[0]), max(ventanas.values())),
                                  fechas[-1])
    ses = _dias(sorted(sesiones))
    ev = _dias(fechas)
    inicio = {(int(d), n): int(_dias([restar_anios(f, n)])[0]) for f, d in zip(fechas, ev) for n in ventanas.values()}
    por_especie: dict[str, list[tuple[int, str]]] = {}
    if quiebres_df is not None and not quiebres_df.is_empty():
        for r in quiebres_df.sort("fecha").iter_rows(named=True):
            por_especie.setdefault(r["especie"], []).append(
                (int(_dias([r["fecha"]])[0]), r["tipo"], r.get("indicador")))

    filas = []
    for (especie,), g in diaria.sort(["especie", "fecha"]).group_by(["especie"], maintain_order=True):
        fd = _dias(g.get_column("fecha").to_list())
        emisor = g.get_column("emisor_id")[0]
        for ind in indicadores:
            eventos = [(e, t) for e, t, i in por_especie.get(especie, []) if i is None or i == ind]
            x = g.get_column(ind).cast(pl.Float64).fill_null(np.nan).to_numpy()
            ok = np.isfinite(x)
            if not ok.any():
                continue
            fv, xv = fd[ok], x[ok]
            for d in ev[(ev >= fv[0]) & (ev <= fd[-1] + reciente)]:
                d = int(d)
                i = int(np.searchsorted(fd, d, side="right")) - 1
                if i < 0 or d - fd[i] > reciente or not np.isfinite(x[i]):
                    continue  # sin negocios recientes, o el indicador no existe ese día (utilidad ≤ 0)
                actual, f_actual = float(x[i]), int(fd[i])
                for nombre_v, n in ventanas.items():
                    desde_nom = inicio[(d, n)]
                    q = next(((e, t) for e, t in reversed(eventos) if desde_nom < e <= d), None)
                    recortada = bool(recortar and q is not None)
                    desde = q[0] if recortada else desde_nom
                    s = xv[np.searchsorted(fv, desde, side="left"):np.searchsorted(fv, d, side="right")]
                    n_obs = int(s.size)
                    n_ses = int(np.searchsorted(ses, d, side="right") - np.searchsorted(ses, desde_nom, side="left"))
                    cobertura = min(1.0, n_obs / n_ses) if n_ses else None
                    media = float(s.mean()) if n_obs else None
                    desv = float(s.std(ddof=1)) if n_obs > 1 else None
                    suficiente = bool(cobertura is not None and cobertura >= minima and n_obs >= min_obs
                                      and desv is not None and desv > 0)
                    z = (actual - media) / desv if suficiente else None
                    pct = float(((s < actual).sum() + 0.5 * (s == actual).sum()) / n_obs) if suficiente else None
                    f_q = _de_dias(q[0]) if q else None
                    filas.append({
                        "fecha": _de_dias(d), "especie": especie, "emisor_id": emisor, "indicador": ind,
                        "ventana": nombre_v, "anios": n, "actual": actual, "fecha_actual": _de_dias(f_actual),
                        "media": media, "desviacion": desv, "z": z, "percentil": pct,
                        "minimo": float(s.min()) if n_obs else None, "maximo": float(s.max()) if n_obs else None,
                        "n_obs": n_obs, "sesiones": n_ses, "fecha_desde": _de_dias(desde), "cobertura": cobertura,
                        "suficiente": suficiente, "quiebre": f_q, "tipo_quiebre": q[1] if q else None,
                        "recortada": recortada,
                        "aviso": _aviso(ind, q[1] if q else None, f_q, recortada, suficiente, cobertura, minima),
                    })
    if not filas:
        return pl.DataFrame(schema=ESQUEMA_BANDA)
    return pl.DataFrame(filas, schema=ESQUEMA_BANDA).sort(["fecha", "especie", "indicador", "anios"])


def construir() -> dict:
    cfg = config()
    diaria = fact_valoracion_diaria()
    if diaria is None or diaria.is_empty():
        return {}
    storage.escribir_por_anio("gold", "fact_valoracion_diaria", diaria, "fecha")
    fechas = fechas_valoracion(diaria.get_column("fecha"))
    saltos = saltos_vpa(diaria, float(cfg.get("salto_vpa", 3)))
    todos = pl.concat([quiebres(), saltos]).sort(["especie", "fecha"])
    storage.escribir_completa("gold", "fact_quiebre_valoracion", todos)
    mensual = bandas_en_fechas(diaria, fechas, todos, cfg=cfg)
    storage.escribir_por_anio("gold", "fact_banda_valoracion_mensual", mensual, "fecha")
    ultimo = mensual.get_column("fecha").max() if not mensual.is_empty() else None
    hoy = mensual.filter(pl.col("fecha") == ultimo) if ultimo else mensual
    storage.escribir_completa("gold", "fact_banda_valoracion", hoy)
    return {"filas_diarias": diaria.height, "filas_mensuales": mensual.height, "fecha": ultimo,
            "especies_hoy": hoy.get_column("especie").n_unique() if hoy.height else 0,
            "con_z_hoy": hoy.filter(pl.col("z").is_not_null()).height,
            "con_quiebre_hoy": hoy.filter(pl.col("quiebre").is_not_null()).height}
