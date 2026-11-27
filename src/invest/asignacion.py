"""Asignación de activos (etapa 3 del plan): supuestos de mercado y los tres métodos.

1. **Supuestos de mercado** (``supuestos``): retorno real esperado a 10 años de cada activo, por
   componentes y con los datos de hoy (``config/supuestos_mercado.yaml``), nunca el promedio histórico:
   - efectivo y CDT: la tasa real de hoy converge a la neutral en unos años;
   - TES y Tesoro a 10 años: la tasa de hoy menos la inflación esperada (quien compra y mantiene un bono
     gana, en promedio, su tasa);
   - dólares y activos en dólares, medidos en pesos: su retorno real en dólares (la devaluación compensa
     la diferencia de inflación a largo plazo), con un rango ancho porque la tasa de cambio domina;
   - acciones colombianas: rendimiento por dividendo + crecimiento real + parte del regreso del P/U a su
     mediana de 10 años.
   Cada supuesto lleva rango y su cuenta; el usuario puede cambiar cualquiera.
2. **Escenarios y covarianza**: retornos mensuales de los últimos 15 años de cada activo en la moneda de
   la cartera, recentrados para que su promedio sea el supuesto (se conserva la forma de la historia:
   volatilidad, colas y correlaciones) y covarianza con encogimiento de Ledoit-Wolf.
3. **Los tres métodos** (skfolio):
   - ``optimizada``: la de mayor retorno esperado cuya volatilidad no pasa la objetivo del perfil, con
     pesos máximos por activo; si el usuario da vistas, el retorno esperado sale de Black-Litterman
     (cada vista con su confianza, método de Idzorek);
   - ``hrp``: paridad de riesgo jerárquica, que reparte el riesgo entre grupos de activos parecidos y
     no usa retornos esperados (la referencia robusta);
   - ``benchmark``: la mezcla simple del perfil (su asignación objetivo, o una por objetivo).
   De cada una: pesos, retorno real esperado, volatilidad, CVaR del peor 5 % de los meses y cuánto del
   riesgo aporta cada activo.

Son carteras modelo para aprender y comparar, con los supuestos a la vista: no son una recomendación
ni una orden. La validación fuera de muestra es la etapa 4.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl

from invest import backtest as bt
from invest import storage
from invest.config import cargar_yaml

METODOS = ("optimizada", "hrp", "benchmark")


def config() -> dict:
    return cargar_yaml("supuestos_mercado.yaml") or {}


# --------------------------------------------------------------------------- supuestos
def _ultimo(serie: pl.DataFrame) -> tuple[float | None, date | None]:
    if serie.is_empty():
        return None, None
    fila = serie.sort("fecha").row(-1, named=True)
    return float(fila["valor"]), fila["fecha"]


def _p(x: float, d: int = 2) -> str:
    return f"{x:.{d}f} %".replace(".", ",")


def _real(nominal: float, inflacion: float) -> float:
    return (1 + nominal) / (1 + inflacion) - 1


def _promedio_convergencia(ahora: float, neutral: float, anios_conv: float, horizonte: float = 10) -> float:
    """Promedio a ``horizonte`` años de una tasa que va en línea recta de ``ahora`` a ``neutral`` en ``anios_conv``."""
    anios_conv = min(anios_conv, horizonte)
    return (anios_conv * (ahora + neutral) / 2 + (horizonte - anios_conv) * neutral) / horizonte


def _acciones_co(cfg: dict, especie: str | None = None) -> tuple[float, list[tuple[str, float]], str]:
    """Rendimiento por dividendo + crecimiento real + regreso parcial del P/U (universo líquido, o una acción)."""
    a = cfg.get("acciones_co", {})
    val = storage.leer_tabla("gold", "fact_valoracion")
    ficha = storage.leer_tabla("gold", "fact_ficha_accion")
    if val is None or val.is_empty():
        raise ValueError("Sin valoración de las acciones (gold/fact_valoracion)")
    if especie:
        especies = [especie]
    elif ficha is not None and not ficha.is_empty():
        f = ficha.filter(pl.col("fecha") == ficha.get_column("fecha").max()) if "fecha" in ficha.columns else ficha
        especies = f.filter(pl.col("liquida")).get_column("especie").unique().to_list()
    else:
        especies = val.get_column("especie").unique().to_list()
    v = val.filter(pl.col("especie").is_in(especies))
    hoy = v.get_column("fecha").max()
    actual = v.sort("fecha").group_by("especie").agg(pl.all().last())
    dy = actual.filter(pl.col("rendimiento_dividendo_12m").is_between(0, 0.3)).get_column("rendimiento_dividendo_12m").median()
    pu_hoy = actual.filter(pl.col("p_u").is_between(0.5, 60)).get_column("p_u").median()
    desde = hoy - timedelta(days=int(365.25 * a.get("anios_historia_pu", 10)))
    hist = (v.filter((pl.col("fecha") >= desde) & pl.col("p_u").is_between(0.5, 60))
            .group_by("fecha").agg(pl.col("p_u").median()).get_column("p_u").median())
    if dy is None or pu_hoy is None or hist is None:
        raise ValueError("Sin rendimiento por dividendo o P/U suficientes para el supuesto de acciones")
    g = float(a.get("crecimiento_real", 0.015))
    f = float(a.get("reversion_valoracion", 0.5))
    valoracion = (hist / pu_hoy) ** (f / 10) - 1
    componentes = [("Rendimiento por dividendo (mediana de hoy)", float(dy)), ("Crecimiento real de largo plazo", g),
                   (f"Regreso del P/U de {pu_hoy:.1f} hacia su mediana de 10 años ({hist:.1f}), la mitad en 10 años".replace(".", ",") if f == 0.5 else f"Regreso del P/U de {pu_hoy:.1f} hacia su mediana de 10 años ({hist:.1f}), {f * 100:.0f} % en 10 años".replace(".", ","), valoracion)]
    nota = (f"{len(especies)} acciones del universo líquido" if not especie else "con los datos de la acción") + f", al {hoy.isoformat()}"
    return float(dy) + g + valoracion, componentes, nota


def supuestos(activos: list[str], datos: bt.Datos | None = None, ajustes: dict[str, float] | None = None) -> dict[str, dict]:
    """{activo: {real, bajo, alto, componentes [(texto, valor)], nota, ajustado}} — retorno real anual esperado a 10 años."""
    cfg = config()
    datos = datos or bt.Datos()
    inf = cfg.get("inflacion_esperada", {})
    pi_cop, pi_usd = float(inf.get("COP", 0.035)), float(inf.get("USD", 0.023))
    neutral_cop, neutral_usd = float(cfg.get("tasa_real_neutral_cop", 0.015)), float(cfg.get("tasa_real_neutral_usd", 0.008))
    conv = float(cfg.get("anios_convergencia_efectivo", 3))
    rangos = cfg.get("rango", {})
    catalogo_bt = (bt.config().get("activos") or {})
    salida = {}
    for aid in activos:
        a = catalogo_bt.get(aid, {})
        clase = a.get("clase") or ("ACCION" if aid.startswith("accion:") else "FIC" if aid.startswith("fic:") else "")
        tipo = a.get("tipo") or ("accion" if aid.startswith("accion:") else "fic" if aid.startswith("fic:") else "")
        componentes, nota = [], ""
        if tipo in ("tasa", "cdt") and a.get("moneda", "COP") == "COP":
            tasa, f = _ultimo(datos.macro(a["series"][0]))
            if tasa is None and len(a["series"]) > 1:
                tasa, f = _ultimo(datos.macro(a["series"][1]))
            if tasa is None:
                continue
            ahora = _real(tasa / 100, pi_cop)
            real = _promedio_convergencia(ahora, neutral_cop, conv)
            componentes = [(f"Tasa de hoy {_p(tasa)} menos la inflación esperada ({_p(pi_cop * 100, 1)})", ahora),
                           (f"Va hacia la tasa real neutral en {conv:.0f} años", neutral_cop)]
            nota = f"tasa al {f.isoformat()}"
        elif tipo == "tasa":  # efectivo en dólares
            tasa, f = _ultimo(datos.macro(a["series"][0]))
            if tasa is None:
                continue
            ahora = _real(tasa / 100, pi_usd)
            real = _promedio_convergencia(ahora, neutral_usd, conv)
            componentes = [(f"Letra del Tesoro hoy {_p(tasa)} menos la inflación de EE. UU. ({_p(pi_usd * 100, 1)})", ahora),
                           (f"Va hacia la tasa real neutral en dólares en {conv:.0f} años", neutral_usd),
                           ("En pesos: la devaluación compensa la diferencia de inflación a largo plazo", 0.0)]
            nota = f"tasa al {f.isoformat()}"
        elif tipo == "bono":
            tasa, f = _ultimo(datos.macro(a["series"][0]))
            if tasa is None:
                continue
            pi = pi_usd if a.get("moneda") == "USD" else pi_cop
            real = _real(tasa / 100, pi)
            componentes = [(f"Tasa a 10 años hoy {_p(tasa)}", tasa / 100), (f"Menos la inflación esperada ({_p(pi * 100, 1)})", -pi)]
            if a.get("moneda") == "USD":
                componentes.append(("En pesos: la devaluación compensa la diferencia de inflación a largo plazo", 0.0))
            nota = f"tasa al {f.isoformat()}"
        elif tipo in ("indice", "accion"):
            try:
                real, componentes, nota = _acciones_co(cfg, aid.split(":", 1)[1] if tipo == "accion" else None)
            except ValueError:
                continue
        elif tipo == "fic":
            base = supuestos(["efectivo_cop"], datos).get("efectivo_cop")
            if not base:
                continue
            real, componentes = base["real"], [("Aproximado como efectivo en pesos (un FIC no tiene supuesto propio)", base["real"])]
            nota = "aproximado"
        else:
            continue
        ancho = float(rangos.get(clase, 0.03))
        fila = {"real": real, "bajo": real - ancho, "alto": real + ancho, "componentes": componentes, "nota": nota,
                "clase": clase, "ajustado": False}
        if ajustes and aid in ajustes and ajustes[aid] is not None:
            fila.update(real=float(ajustes[aid]), ajustado=True)
        salida[aid] = fila
    return salida


# --------------------------------------------------------------------------- escenarios
def retornos_mensuales(activos: list[str], moneda: str, datos: bt.Datos, anios: int) -> tuple[np.ndarray, list[date], list[str]]:
    """Retornos mensuales de los últimos ``anios`` años, en la moneda de la cartera, en el periodo común."""
    hoy = date.today()
    fin = date(hoy.year, hoy.month, 1) - timedelta(days=1)
    meses = bt.meses_entre(date(fin.year - anios, fin.month, 1), fin)
    trm = bt._al_fin_de_mes(datos.trm(), meses)
    columnas, avisos = [], []
    for a in activos:
        nivel, _ = bt.niveles(a, meses, datos)
        columnas.append(bt.en_moneda(nivel, bt.activo(a).moneda, moneda, trm))
    m = np.column_stack(columnas)
    validos = ~np.isnan(m).any(axis=1)
    k0 = int(np.flatnonzero(validos)[0]) if validos.any() else len(meses)
    m, meses = m[k0:], meses[k0:]
    r = m[1:] / m[:-1] - 1
    return r, meses[1:], avisos


def _mensual(anual_nominal: float) -> float:
    return (1 + anual_nominal) ** (1 / 12) - 1


def nominal(real: float, moneda: str) -> float:
    inf = config().get("inflacion_esperada", {})
    return (1 + real) * (1 + float(inf.get(moneda, 0.035))) - 1


# --------------------------------------------------------------------------- los tres métodos
def _benchmark(perfil: dict | None, asignacion: list[dict], activos: list[str]) -> tuple[dict[str, float], str]:
    cfg = config()
    mapa = cfg.get("clase_a_activo", {})
    if asignacion:
        pesos: dict[str, float] = {}
        for x in asignacion:
            destino = mapa.get(x["clase_activo_id"])
            if destino:
                pesos[destino] = pesos.get(destino, 0.0) + float(x["peso_objetivo"])
        total = sum(pesos.values())
        if total > 0:
            return {k: v / total for k, v in pesos.items()}, "la asignación objetivo del perfil"
    objetivo = (perfil or {}).get("objetivo") or "crecimiento"
    base = cfg.get("benchmark_por_objetivo", {}).get(objetivo) or {"acciones_co_igual": 0.6, "tes_10a": 0.4}
    return {k: float(v) for k, v in base.items()}, f"la de referencia para el objetivo «{objetivo}» (config/supuestos_mercado.yaml)"


def calcular(activos: list[str] | None = None, moneda: str = "COP", volatilidad_objetivo: float = 0.10,
             maximo_por_activo: float = 0.6, vistas: list[dict] | None = None, ajustes: dict[str, float] | None = None,
             perfil: dict | None = None, asignacion_perfil: list[dict] | None = None, datos: bt.Datos | None = None) -> dict:
    """Las tres carteras. vistas: [{activo, retorno_real (anual), confianza (0–1)}]; ajustes: {activo: retorno real anual}."""
    import pandas as pd
    from skfolio import RiskMeasure
    from skfolio.moments import LedoitWolf
    from skfolio.optimization import HierarchicalRiskParity, MeanRisk, ObjectiveFunction
    from skfolio.prior import BlackLitterman, EmpiricalPrior

    cfg = config()
    datos = datos or bt.Datos()
    activos = list(dict.fromkeys(activos or cfg.get("activos_por_defecto") or []))
    if not 0.01 <= volatilidad_objetivo <= 0.6:
        raise ValueError("La volatilidad objetivo debe estar entre 1 % y 60 % anual")
    if not 0.05 <= maximo_por_activo <= 1:
        raise ValueError("El máximo por activo debe estar entre 5 % y 100 %")
    w_bench, nota_bench = _benchmark(perfil, asignacion_perfil or [], activos)
    sup = supuestos(list(dict.fromkeys(activos + list(w_bench))), datos, ajustes)
    avisos = [f"{a}: sin supuesto (faltan datos); queda por fuera." for a in activos if a not in sup]
    activos = [a for a in activos if a in sup]
    if any(a not in sup for a in w_bench):
        avisos.append("El benchmark usa activos sin supuesto; se compara solo con lo que tiene datos.")
        w_bench = {a: v for a, v in w_bench.items() if a in sup}
        w_bench = {a: v / sum(w_bench.values()) for a, v in w_bench.items()}
    todos = list(dict.fromkeys(activos + list(w_bench)))
    if len(activos) < 2:
        raise ValueError("Se necesitan al menos dos activos con supuesto")
    if maximo_por_activo * len(activos) < 1:
        maximo_por_activo = 1 / len(activos)
        avisos.append(f"Con {len(activos)} activos el máximo por activo no puede ser menor que {maximo_por_activo * 100:.0f} %; se ajustó.")
    r_todos, meses, _ = retornos_mensuales(todos, moneda, datos, int(cfg.get("historia_covarianza_anios", 15)))
    r = r_todos[:, [todos.index(a) for a in activos]]
    if len(r) < 60:
        avisos.append(f"Solo hay {len(r)} meses en común entre los activos: la covarianza es poco confiable.")
    if len(r) < 24:
        raise ValueError("Menos de dos años de historia en común entre los activos elegidos")
    mu_todos = np.array([_mensual(nominal(sup[a]["real"], moneda)) for a in todos])
    escenarios_todos = r_todos - r_todos.mean(axis=0) + mu_todos  # la forma de la historia, con el promedio de los supuestos
    escenarios = escenarios_todos[:, [todos.index(a) for a in activos]]
    X = pd.DataFrame(escenarios, columns=activos)
    prior = EmpiricalPrior(covariance_estimator=LedoitWolf())
    if vistas:
        textos, confianzas = [], []
        for v in vistas:
            if v.get("activo") not in activos:
                avisos.append(f"Vista sobre {v.get('activo')}: el activo no está en la asignación; se omite.")
                continue
            textos.append(f"{v['activo']} == {_mensual(nominal(float(v['retorno_real']), moneda)):.8f}")
            confianzas.append(min(max(float(v.get("confianza", 0.5)), 0.01), 0.99))
        if textos:
            prior = BlackLitterman(views=textos, view_confidences=confianzas,
                                   prior_estimator=EmpiricalPrior(covariance_estimator=LedoitWolf()))
    tope = volatilidad_objetivo / math.sqrt(12)
    opt = MeanRisk(objective_function=ObjectiveFunction.MAXIMIZE_RETURN, risk_measure=RiskMeasure.CVAR,
                   max_standard_deviation=tope, max_weights=maximo_por_activo, prior_estimator=prior, raise_on_failure=False)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # si no hay solución se usa la de mínima volatilidad (abajo)
        opt.fit(X)
    w_opt = getattr(opt, "weights_", None)
    nota_opt = f"La de mayor retorno esperado con volatilidad de hasta {volatilidad_objetivo * 100:.0f} % anual y máximo {maximo_por_activo * 100:.0f} % por activo."
    if w_opt is None or np.isnan(w_opt).any():
        minimo = MeanRisk(objective_function=ObjectiveFunction.MINIMIZE_RISK, risk_measure=RiskMeasure.VARIANCE,
                          max_weights=maximo_por_activo, prior_estimator=prior)
        minimo.fit(X)
        w_opt = minimo.weights_
        nota_opt = ("Ninguna mezcla llega a una volatilidad tan baja: se muestra la de mínima volatilidad posible "
                    f"con máximo {maximo_por_activo * 100:.0f} % por activo.")
    hrp = HierarchicalRiskParity(risk_measure=RiskMeasure.VARIANCE, max_weights=maximo_por_activo,
                                 prior_estimator=EmpiricalPrior(covariance_estimator=LedoitWolf()))
    hrp.fit(X)
    faltan = [a for a in w_bench if a not in activos]
    if faltan:
        avisos.append("El benchmark usa activos que no están en la lista (" + ", ".join(faltan) + "); se agregan solo para él.")
    # Covarianza encogida (de los escenarios) para la volatilidad y la contribución al riesgo
    sigma = LedoitWolf().fit(escenarios_todos).covariance_
    salida_metodos = {}
    for nombre, w, nota in (("optimizada", np.asarray(w_opt, dtype=float), nota_opt),
                            ("hrp", np.asarray(hrp.weights_, dtype=float),
                             "Reparte el riesgo entre grupos de activos que se mueven parecido; no usa los retornos esperados."),
                            ("benchmark", None, f"Mezcla simple: {nota_bench}.")):
        if w is None:
            ids = list(w_bench)
            pesos = w_bench
        else:
            w = np.where(w < 1e-4, 0.0, w)
            w = w / w.sum()
            ids = activos
            pesos = {a: float(x) for a, x in zip(activos, w) if x > 0}
        salida_metodos[nombre] = {"pesos": pesos, "nota": nota,
                                  **_metricas(pesos, todos, sup, escenarios_todos, sigma, moneda, datos)}
    return {
        "moneda": moneda, "activos": activos, "periodo_escenarios": {"desde": meses[0].isoformat(), "hasta": meses[-1].isoformat(), "meses": len(meses)},
        "supuestos": {a: {**s, "nominal": nominal(s["real"], moneda)} for a, s in sup.items()},
        "inflacion_esperada": config().get("inflacion_esperada", {}),
        "parametros": {"volatilidad_objetivo": volatilidad_objetivo, "maximo_por_activo": maximo_por_activo, "vistas": vistas or []},
        "metodos": salida_metodos, "avisos": avisos,
        "nombres": {a: _nombre(a, datos) for a in todos},
    }


def _nombre(aid: str, datos: bt.Datos) -> str:
    try:
        return bt.activo(aid).nombre
    except ValueError:
        return aid


def _metricas(pesos: dict[str, float], activos: list[str], sup: dict, escenarios: np.ndarray, sigma: np.ndarray,
              moneda: str, datos: bt.Datos) -> dict:
    """Retorno real esperado, volatilidad, CVaR del peor 5 % de los meses y contribución al riesgo."""
    dentro = [a for a in pesos if a in activos]
    w = np.array([pesos.get(a, 0.0) for a in activos])
    salida: dict = {}
    if dentro and abs(w.sum() - 1) < 1e-6:
        var = float(w @ sigma @ w)
        salida["volatilidad"] = math.sqrt(var) * math.sqrt(12)
        port = escenarios @ w
        peor = np.sort(port)[: max(1, int(0.05 * len(port)))]
        salida["cvar_95_mensual"] = float(peor.mean())
        contrib = w * (sigma @ w) / var if var > 0 else w
        salida["contribucion_riesgo"] = {a: float(c) for a, c in zip(activos, contrib) if w[activos.index(a)] > 0}
    reales = {a: (sup[a]["real"] if a in sup else None) for a in pesos}
    if all(v is not None for v in reales.values()):
        salida["retorno_real"] = sum(pesos[a] * reales[a] for a in pesos)
        salida["retorno_real_bajo"] = sum(pesos[a] * sup[a]["bajo"] for a in pesos)
        salida["retorno_real_alto"] = sum(pesos[a] * sup[a]["alto"] for a in pesos)
    return salida
