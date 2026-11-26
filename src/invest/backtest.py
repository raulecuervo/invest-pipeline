"""Backtesting de carteras por reglas (etapa 1 del plan de asignación de activos).

Una cartera es un conjunto de activos con pesos objetivo. Se simula mes a mes con la historia:

1. **Niveles mensuales.** Cada activo es una serie de valor con dividendos e intereses reinvertidos,
   tomada al último dato de cada mes (fin de mes de calendario): acciones (``accion:<especie>``, índice
   de retorno total limpio de ``gold/fact_tecnico_diario``), índices propios del universo líquido y los
   activos sintéticos de ``config/backtest.yaml`` (efectivo que crece con una tasa diaria, bonos de plazo
   constante aproximados desde su tasa). Lo que está en otra moneda se convierte con la TRM del fin de
   mes, así que un activo en dólares en una cartera en pesos incluye la variación del dólar.
2. **Periodo común.** El backtest empieza cuando todos los activos tienen dato y termina en el último
   mes en que todos lo tienen (o en las fechas pedidas, dentro de ese rango).
3. **Simulación.** Cada mes los valores crecen con su retorno y los pesos se desvían. Se rebalancea a
   los pesos objetivo según la regla (mensual, trimestral, anual, por bandas o nunca); lo comprado y lo
   vendido paga el costo de cada activo. Los aportes mensuales entran a los pesos objetivo (pagan costo).
4. **Métricas** sobre el índice de la cartera (retorno ponderado por tiempo, así los aportes no
   cuentan como rentabilidad): retorno anual nominal y real (IPC de Colombia en pesos, de EE. UU. en
   dólares), volatilidad, caída máxima y meses para recuperarse, Sharpe y Sortino frente al efectivo de
   la moneda, CVaR del peor 5 % de los meses, rotación y costos; y los retornos de todas las ventanas
   de 3, 5 y 10 años, porque el resultado de un solo periodo depende mucho de dónde empieza.

Es una prueba con la historia: no dice qué va a pasar ni qué comprar. No mira al futuro (cada mes usa
solo el retorno de ese mes y la regla se decide con los pesos de ese momento).
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl

from invest import storage
from invest.config import cargar_yaml

REBALANCEOS = ("mensual", "trimestral", "anual", "bandas", "nunca")
MONEDAS = ("COP", "USD")
INFLACION = {"COP": "ipc_co", "USD": "fred_cpiaucsl"}
EFECTIVO = {"COP": "efectivo_cop", "USD": "efectivo_usd"}
VENTANAS_ANIOS = (3, 5, 10)


def config() -> dict:
    return cargar_yaml("backtest.yaml") or {}


def fin_de_mes(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def meses_entre(desde: date, hasta: date) -> list[date]:
    salida, y, m = [], desde.year, desde.month
    while (y, m) <= (hasta.year, hasta.month):
        salida.append(date(y, m, calendar.monthrange(y, m)[1]))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return salida


# --------------------------------------------------------------------------- series de los activos
class Datos:
    """Lee una sola vez las tablas que necesitan los activos (el aplicativo lo reutiliza)."""

    def __init__(self) -> None:
        self._macro: pl.DataFrame | None = None
        self._tecnico: pl.DataFrame | None = None
        self._indices: pl.DataFrame | None = None
        self._trm: pl.DataFrame | None = None

    def macro(self, serie_id: str) -> pl.DataFrame:
        if self._macro is None:
            t = storage.leer_tabla("gold", "fact_macro")
            self._macro = pl.DataFrame(schema={"fecha": pl.Date, "serie_id": pl.Utf8, "valor": pl.Float64}) \
                if t is None else t.select("fecha", "serie_id", "valor")
        return (self._macro.filter(pl.col("serie_id") == serie_id).select("fecha", "valor")
                .drop_nulls().unique("fecha", keep="last").sort("fecha"))

    def tecnico(self) -> pl.DataFrame:
        if self._tecnico is None:
            t = storage.leer_tabla("gold", "fact_tecnico_diario")
            self._tecnico = (pl.DataFrame(schema={"fecha": pl.Date, "especie": pl.Utf8, "valor": pl.Float64}) if t is None
                             else t.select("fecha", "especie", pl.col("indice_rt_limpio").alias("valor")).drop_nulls().sort("fecha"))
        return self._tecnico

    def indice(self, indice_id: str) -> pl.DataFrame:
        if self._indices is None:
            t = storage.leer_tabla("gold", "fact_indice_mercado")
            self._indices = pl.DataFrame(schema={"fecha": pl.Date, "indice_id": pl.Utf8, "moneda": pl.Utf8, "valor": pl.Float64}) \
                if t is None else t.select("fecha", "indice_id", "moneda", "valor")
        return (self._indices.filter((pl.col("indice_id") == indice_id) & (pl.col("moneda") == "COP"))
                .select("fecha", "valor").drop_nulls().sort("fecha"))

    def fic(self) -> pl.DataFrame:
        if getattr(self, "_fic", None) is None:
            t = storage.leer_tabla("silver", "fact_fic_mensual")
            self._fic = pl.DataFrame(schema={"fecha": pl.Date, "id": pl.Utf8, "valor": pl.Float64, "nombre_fondo": pl.Utf8,
                                             "nombre_entidad": pl.Utf8, "subtipo": pl.Utf8, "valor_fondo": pl.Float64}) if t is None else \
                t.select("fecha", pl.concat_str([pl.lit("fic:"), "codigo_entidad", pl.lit(":"), "codigo_negocio", pl.lit(":"),
                                                 "tipo_participacion"]).alias("id"), pl.col("valor_unidad").alias("valor"),
                         "nombre_fondo", "nombre_entidad", "subtipo", "valor_fondo").sort("fecha")
        return self._fic

    def trm(self) -> pl.DataFrame:
        if self._trm is None:
            from invest.mercado import _trm
            self._trm = _trm().rename({"fecha_mercado": "fecha", "trm": "valor"})
        return self._trm


def _al_fin_de_mes(diaria: pl.DataFrame, meses: list[date], tolerancia_dias: int | None = None) -> np.ndarray:
    """Último valor conocido a cada fin de mes (NaN antes del primer dato o si es más viejo que la tolerancia)."""
    if diaria.is_empty():
        return np.full(len(meses), np.nan)
    f = diaria.get_column("fecha").to_numpy().astype("datetime64[D]")
    v = diaria.get_column("valor").to_numpy().astype(float)
    m = np.array(meses, dtype="datetime64[D]")
    i = np.searchsorted(f, m, side="right") - 1
    salida = np.where(i >= 0, v[np.clip(i, 0, None)], np.nan)
    if tolerancia_dias is not None:
        viejo = (i >= 0) & ((m - f[np.clip(i, 0, None)]).astype(int) > tolerancia_dias)
        salida = np.where(viejo, np.nan, salida)
    return salida


def nivel_tasa(diaria: pl.DataFrame, meses: list[date]) -> np.ndarray:
    """Valor de una inversión que cada día gana la tasa anual efectiva vigente (en %), base 1 al primer
    fin de mes con tasa. La tasa del día t rige hasta el día siguiente con dato."""
    if diaria.is_empty():
        return np.full(len(meses), np.nan)
    f = diaria.get_column("fecha").to_list()
    r = diaria.get_column("valor").to_list()
    # Valor acumulado en cada fecha con dato
    acumulado = [1.0]
    for k in range(1, len(f)):
        dias = (f[k] - f[k - 1]).days
        acumulado.append(acumulado[-1] * (1 + r[k - 1] / 100) ** (dias / 365))
    salida = []
    for m in meses:
        j = _ultimo_indice(f, m)
        if j is None:
            salida.append(np.nan)
            continue
        dias = (m - f[j]).days
        salida.append(acumulado[j] * (1 + r[j] / 100) ** (dias / 365))
    return np.array(salida, dtype=float)


def nivel_cdt(diaria: pl.DataFrame, meses: list[date], plazo_dias: int) -> np.ndarray:
    """Valor de un CDT que se renueva al vencer: la tasa queda fija ``plazo_dias`` desde la compra y al
    renovarse toma la tasa vigente ese día (antes de impuestos). Base 1 en la primera compra."""
    if diaria.is_empty():
        return np.full(len(meses), np.nan)
    f = diaria.get_column("fecha").to_list()
    r = diaria.get_column("valor").to_list()
    salida = np.full(len(meses), np.nan)
    compra, valor_compra, tasa = f[0], 1.0, r[0]
    for k, m in enumerate(meses):
        if m < f[0]:
            continue
        while (m - compra).days >= plazo_dias:  # vence y se renueva a la tasa de ese día
            vence = compra + timedelta(days=plazo_dias)
            valor_compra *= (1 + tasa / 100) ** (plazo_dias / 365)
            j = _ultimo_indice(f, vence)
            compra, tasa = vence, r[j]
        salida[k] = valor_compra * (1 + tasa / 100) ** ((m - compra).days / 365)
    return salida


def _ultimo_indice(fechas: list[date], d: date) -> int | None:
    import bisect
    i = bisect.bisect_right(fechas, d) - 1
    return i if i >= 0 else None


def _precio(cupon: float, tasa: float, plazo: float) -> float:
    """Precio por 1 de nominal: cupones anuales ``cupon`` y principal, descontados a ``tasa``, con ``plazo``
    años (fraccionario: el cupón corre en proporción al tiempo, una aproximación suave)."""
    if abs(tasa) < 1e-9:
        return cupon * plazo + 1.0
    descuento = (1 + tasa) ** -plazo
    return cupon * (1 - descuento) / tasa + descuento


def nivel_bono(tasas_mes: np.ndarray, plazo: float) -> np.ndarray:
    """Índice de un bono de plazo constante: cada mes se compra uno a la par (cupón = tasa del mes) y al
    mes siguiente se valora con la tasa nueva y un mes menos de plazo, más el cupón corrido del mes."""
    salida = np.full(len(tasas_mes), np.nan)
    nivel = None
    for k, y in enumerate(tasas_mes):
        if np.isnan(y):
            nivel = None  # un hueco corta la serie: vuelve a empezar en 1 cuando haya tasa
            continue
        if nivel is None:
            nivel = 1.0
        elif not np.isnan(tasas_mes[k - 1]):
            c, y1 = tasas_mes[k - 1] / 100, y / 100
            retorno = _precio(c, y1, plazo - 1 / 12) + c / 12 - 1
            nivel *= 1 + retorno
        salida[k] = nivel
    return salida


@dataclass
class Activo:
    id: str
    nombre: str
    clase: str
    moneda: str
    costo: float
    aproximado: bool = False
    nota: str = ""


def catalogo(datos: Datos | None = None) -> list[dict]:
    """Activos disponibles para el Laboratorio: los de config/backtest.yaml y cada acción con retorno total."""
    cfg = config()
    datos = datos or Datos()
    salida = []
    for aid, a in (cfg.get("activos") or {}).items():
        salida.append({"id": aid, "nombre": a["nombre"], "clase": a.get("clase"), "moneda": a.get("moneda", "COP"),
                       "costo": a.get("costo", 0.0), "aproximado": bool(a.get("aproximado")), "nota": a.get("nota", ""),
                       "tipo": a.get("tipo")})
    t = datos.tecnico()
    if not t.is_empty():
        rangos = t.group_by("especie").agg(pl.col("fecha").min().alias("desde"), pl.col("fecha").max().alias("hasta"),
                                           pl.len().alias("dias"))
        for r in rangos.sort("especie").iter_rows(named=True):
            salida.append({"id": f"accion:{r['especie']}", "nombre": r["especie"], "clase": "ACCION", "moneda": "COP",
                           "costo": cfg.get("costo_accion", 0.005), "aproximado": False, "nota": "", "tipo": "accion",
                           "desde": r["desde"].isoformat(), "hasta": r["hasta"].isoformat(), "dias": r["dias"]})
    fic = datos.fic()
    if not fic.is_empty():
        ultimo = fic.get_column("fecha").max()
        rangos = fic.group_by("id").agg(pl.col("fecha").min().alias("desde"), pl.col("fecha").max().alias("hasta"),
                                        pl.col("nombre_fondo").last(), pl.col("nombre_entidad").last(), pl.col("subtipo").last(),
                                        pl.col("valor_fondo").last())
        # Solo los que siguen reportando (hasta el último mes con datos)
        for r in rangos.filter(pl.col("hasta") >= ultimo - timedelta(days=40)).sort("valor_fondo", descending=True, nulls_last=True).iter_rows(named=True):
            part = r["id"].rsplit(":", 1)[-1]
            salida.append({"id": r["id"], "nombre": f"{_titulo(r['nombre_fondo'])} · {r['nombre_entidad']} (part. {part})",
                           "clase": "FIC", "moneda": "COP", "costo": cfg.get("costo_fic", 0.0), "aproximado": False,
                           "nota": r["subtipo"] or "", "tipo": "fic", "desde": r["desde"].isoformat(), "hasta": r["hasta"].isoformat(),
                           "valor_fondo": r["valor_fondo"]})
    return salida


def _titulo(nombre: str | None) -> str:
    """«FONDO DE INVERSIÓN COLECTIVA ABIERTO X» → «Fondo de inversión colectiva abierto X» (más corto de leer)."""
    t = (nombre or "").strip()
    return t[:1] + t[1:].lower() if t.isupper() else t


def activo(aid: str) -> Activo:
    cfg = config()
    if aid.startswith("accion:"):
        return Activo(aid, aid.split(":", 1)[1], "ACCION", "COP", float(cfg.get("costo_accion", 0.005)))
    if aid.startswith("fic:"):
        return Activo(aid, aid, "FIC", "COP", float(cfg.get("costo_fic", 0.0)),
                      nota="Valor de la unidad a fin de mes, ya neto de la comisión de administración.")
    a = (cfg.get("activos") or {}).get(aid)
    if a is None:
        raise ValueError(f"Activo desconocido: {aid}")
    return Activo(aid, a["nombre"], a.get("clase", ""), a.get("moneda", "COP"), float(a.get("costo", 0.0)),
                  bool(a.get("aproximado")), a.get("nota", ""))


def niveles(aid: str, meses: list[date], datos: Datos) -> tuple[np.ndarray, np.ndarray | None]:
    """(nivel en la moneda del activo a cada fin de mes, meses sin negocio para acciones)."""
    cfg = config()
    if aid.startswith("accion:"):
        especie = aid.split(":", 1)[1]
        d = datos.tecnico().filter(pl.col("especie") == especie).select("fecha", "valor")
        if d.is_empty():
            raise ValueError(f"Sin historia de retorno total para {especie}")
        nivel = _al_fin_de_mes(d, meses)
        # Hasta el último negocio: después de la última fecha con datos la acción ya no está disponible
        ultimo = d.get_column("fecha").max()
        nivel = np.where(np.array(meses, dtype="datetime64[D]") > np.datetime64(fin_de_mes(ultimo)), np.nan, nivel)
        # Días desde el último negocio a cada fin de mes
        ultimo_negocio = _al_fin_de_mes(d.select("fecha", pl.col("fecha").cast(pl.Int32).cast(pl.Float64).alias("valor")), meses)
        mes_num = np.array([(m - date(1970, 1, 1)).days for m in meses], dtype=float)
        sin_negocio = (mes_num - ultimo_negocio) > 31
        return nivel, sin_negocio
    if aid.startswith("fic:"):
        d = datos.fic().filter(pl.col("id") == aid).select("fecha", "valor")
        if d.is_empty():
            raise ValueError(f"Sin historia del fondo {aid}")
        nivel = _al_fin_de_mes(d, meses, tolerancia_dias=10)  # un mes sin reporte queda como hueco
        return nivel, None
    a = (cfg.get("activos") or {}).get(aid)
    if a is None:
        raise ValueError(f"Activo desconocido: {aid}")
    tipo = a.get("tipo")
    if tipo == "cdt":
        return nivel_cdt(datos.macro(a["series"][0]), meses, int(a.get("plazo_dias", 90))), None
    if tipo == "indice":
        return _al_fin_de_mes(datos.indice(a["indice_id"]), meses), None
    if tipo == "tasa":
        series = [datos.macro(s) for s in a["series"]]
        # La primera serie manda donde tiene dato; antes de su inicio se usan las siguientes
        partes = []
        for k, s in enumerate(series):
            if s.is_empty():
                continue
            inicio_mejor = min((x.get_column("fecha").min() for x in series[:k] if not x.is_empty()), default=None)
            partes.append(s if inicio_mejor is None else s.filter(pl.col("fecha") < inicio_mejor))
        diaria = pl.concat(partes).sort("fecha") if partes else pl.DataFrame(schema={"fecha": pl.Date, "valor": pl.Float64})
        return nivel_tasa(diaria, meses), None
    if tipo == "bono":
        tasa = _al_fin_de_mes(datos.macro(a["series"][0]), meses, tolerancia_dias=45)
        return nivel_bono(tasa, float(a.get("plazo_anios", 10))), None
    raise ValueError(f"Tipo de activo no soportado: {tipo}")


def en_moneda(nivel: np.ndarray, desde: str, hacia: str, trm: np.ndarray) -> np.ndarray:
    if desde == hacia:
        return nivel
    return nivel * trm if (desde, hacia) == ("USD", "COP") else nivel / trm


# --------------------------------------------------------------------------- simulación
@dataclass
class Parametros:
    pesos: dict[str, float]
    rebalanceo: str = "anual"
    banda: float = 0.05            # puntos de peso (0,05 = 5 puntos) para el rebalanceo por bandas
    moneda: str = "COP"
    desde: date | None = None
    hasta: date | None = None
    valor_inicial: float = 100_000_000.0
    aporte_mensual: float = 0.0
    con_costos: bool = True

    def validar(self) -> None:
        if not self.pesos:
            raise ValueError("La cartera no tiene activos")
        if any(w < 0 for w in self.pesos.values()):
            raise ValueError("Los pesos no pueden ser negativos (no hay ventas en corto)")
        total = sum(self.pesos.values())
        if not math.isclose(total, 1.0, abs_tol=0.005):
            raise ValueError(f"Los pesos suman {total * 100:.1f} %; deben sumar 100 %")
        if self.rebalanceo not in REBALANCEOS:
            raise ValueError(f"Rebalanceo no válido: {self.rebalanceo}")
        if self.moneda not in MONEDAS:
            raise ValueError(f"Moneda no válida: {self.moneda}")
        if not 0 < self.banda < 1:
            raise ValueError("La banda debe estar entre 0 y 100 puntos")
        if self.valor_inicial <= 0 or self.aporte_mensual < 0:
            raise ValueError("El valor inicial debe ser positivo y el aporte no puede ser negativo")


@dataclass
class Resultado:
    meses: list[date]
    valor: np.ndarray              # valor de la cartera (con aportes)
    indice: np.ndarray             # retorno ponderado por tiempo, base 100
    pesos: np.ndarray              # pesos al cierre de cada mes (meses × activos)
    activos: list[str]
    rebalanceos: list[date]
    rotacion: float                # suma de lo comprado y vendido / valor promedio, por año
    costos: float                  # costos pagados / valor promedio, por año
    aportes: float
    avisos: list[str] = field(default_factory=list)


def _toca(regla: str, mes: date) -> bool:
    return {"mensual": True, "trimestral": mes.month in (3, 6, 9, 12), "anual": mes.month == 12}.get(regla, False)


def simular(niveles_mes: np.ndarray, meses: list[date], activos: list[str], params: Parametros,
            costos: np.ndarray) -> Resultado:
    """niveles_mes: meses × activos, en la moneda de la cartera, sin huecos."""
    w = np.array([params.pesos[a] for a in activos], dtype=float)
    w = w / w.sum()
    c = costos if params.con_costos else np.zeros(len(activos))
    n = len(meses)
    valores = params.valor_inicial * w * (1 - c)  # la compra inicial paga costo
    costo_total = float(params.valor_inicial * (w * c).sum())
    movido = float(params.valor_inicial)
    serie_valor, serie_indice, serie_pesos, rebal = [valores.sum()], [100.0], [valores / valores.sum()], []
    indice = 100.0
    for k in range(1, n):
        r = niveles_mes[k] / niveles_mes[k - 1] - 1
        antes = valores.sum()
        valores = valores * (1 + r)
        despues = valores.sum()
        indice *= despues / antes
        if params.aporte_mensual > 0:
            compra = params.aporte_mensual * w
            costo = float((compra * c).sum())
            valores = valores + compra * (1 - c)
            costo_total += costo
            movido += params.aporte_mensual
        actuales = valores / valores.sum()
        hacer = _toca(params.rebalanceo, meses[k]) or (
            params.rebalanceo == "bandas" and bool(np.any(np.abs(actuales - w) > params.banda + 1e-12)))
        if hacer and params.rebalanceo != "nunca":
            total = valores.sum()
            # Costo sobre lo que se mueve; se resuelve una vez (el costo es pequeño frente al total)
            objetivo = total * w
            mov = np.abs(objetivo - valores)
            costo = float((mov * c).sum())
            objetivo = (total - costo) * w
            mov = np.abs(objetivo - valores)
            if mov.sum() > 1e-9 * total:
                costo_total += costo
                movido += float(mov.sum())
                rebal.append(meses[k])
                # el costo del rebalanceo reduce el índice (no es un retiro)
                indice *= (total - costo) / total
                valores = objetivo
        serie_valor.append(valores.sum())
        serie_indice.append(indice)
        serie_pesos.append(valores / valores.sum())
    valor = np.array(serie_valor)
    anios = max((n - 1) / 12, 1 / 12)
    promedio = float(valor.mean())
    aportes = params.aporte_mensual * (n - 1)
    return Resultado(meses, valor, np.array(serie_indice), np.array(serie_pesos), activos, rebal,
                     rotacion=(movido - params.valor_inicial - aportes) / promedio / anios,
                     costos=costo_total / promedio / anios, aportes=aportes)


# --------------------------------------------------------------------------- métricas
def _anualizado(factor: float, meses: int) -> float | None:
    if meses <= 0 or factor <= 0:
        return None
    return float(factor ** (12 / meses) - 1)


def metricas(indice: np.ndarray, meses: list[date], efectivo: np.ndarray | None, inflacion: np.ndarray | None) -> dict:
    r = indice[1:] / indice[:-1] - 1
    n = len(r)
    salida: dict = {"meses": n, "desde": meses[0].isoformat(), "hasta": meses[-1].isoformat()}
    if n < 2:
        return salida
    salida["retorno_total"] = float(indice[-1] / indice[0] - 1)
    salida["retorno_anual"] = _anualizado(indice[-1] / indice[0], n)
    if inflacion is not None and not np.isnan(inflacion[-1]):
        k0 = int(np.flatnonzero(~np.isnan(inflacion))[0])  # primer mes con índice de precios
        if n - k0 >= 12:
            salida["retorno_real_anual"] = _anualizado((indice[-1] / indice[k0]) / (inflacion[-1] / inflacion[k0]), n - k0)
            salida["inflacion_anual"] = _anualizado(inflacion[-1] / inflacion[k0], n - k0)
            if k0:
                salida["real_desde"] = meses[k0].isoformat()
    salida["volatilidad_anual"] = float(np.std(r, ddof=1) * math.sqrt(12))
    pico = np.maximum.accumulate(indice)
    caida = indice / pico - 1
    k_valle = int(np.argmin(caida))
    k_pico = int(np.argmax(indice[: k_valle + 1]))
    salida["caida_maxima"] = float(caida[k_valle])
    salida["caida_desde"] = meses[k_pico].isoformat()
    salida["caida_hasta"] = meses[k_valle].isoformat()
    recupero = next((k for k in range(k_valle, len(indice)) if indice[k] >= indice[k_pico]), None)
    salida["meses_recuperacion"] = None if recupero is None else recupero - k_pico
    if efectivo is not None and not np.isnan(efectivo).any():
        rf = efectivo[1:] / efectivo[:-1] - 1
        exceso = r - rf
        sd = np.std(exceso, ddof=1)
        salida["sharpe"] = float(exceso.mean() * 12 / (sd * math.sqrt(12))) if sd > 0 else None
        abajo = math.sqrt(float(np.mean(np.minimum(exceso, 0) ** 2)))
        salida["sortino"] = float(exceso.mean() * 12 / (abajo * math.sqrt(12))) if abajo > 0 else None
    peor = np.sort(r)[: max(1, int(math.floor(0.05 * n)))]
    salida["cvar_95_mensual"] = float(peor.mean())
    salida["peor_mes"] = float(r.min())
    salida["mejor_mes"] = float(r.max())
    salida["meses_positivos"] = float((r > 0).mean())
    anuales = _retornos_anuales(indice, meses)
    if anuales:
        salida["peor_anio"] = min(anuales, key=lambda x: x[1])
        salida["mejor_anio"] = max(anuales, key=lambda x: x[1])
    return salida


def _retornos_anuales(indice: np.ndarray, meses: list[date]) -> list[tuple[int, float]]:
    """Retorno de cada año calendario completo (de diciembre a diciembre)."""
    dic = {m.year: k for k, m in enumerate(meses) if m.month == 12}
    return [(y, float(indice[dic[y]] / indice[dic[y - 1]] - 1)) for y in sorted(dic) if y - 1 in dic]


def ventanas(indice: np.ndarray, inflacion: np.ndarray | None) -> list[dict]:
    """Retorno anual (nominal y real) de todas las ventanas de 3, 5 y 10 años que caben en el periodo."""
    salida = []
    for anios in VENTANAS_ANIOS:
        h = 12 * anios
        if len(indice) <= h:
            continue
        nominal = (indice[h:] / indice[:-h]) ** (1 / anios) - 1
        fila = {"anios": anios, "ventanas": int(len(nominal)), "peor": float(nominal.min()), "mediana": float(np.median(nominal)),
                "mejor": float(nominal.max()), "negativas": float((nominal < 0).mean())}
        if inflacion is not None:
            real = ((indice[h:] / indice[:-h]) / (inflacion[h:] / inflacion[:-h])) ** (1 / anios) - 1
            real = real[~np.isnan(real)]
            if len(real):
                fila.update(peor_real=float(real.min()), mediana_real=float(np.median(real)),
                            reales_negativas=float((real < 0).mean()), ventanas_reales=int(len(real)))
        salida.append(fila)
    return salida


def caidas(indice: np.ndarray) -> np.ndarray:
    return indice / np.maximum.accumulate(indice) - 1


# --------------------------------------------------------------------------- punto de entrada
def _inflacion(moneda: str, meses: list[date], datos: Datos) -> np.ndarray:
    """Índice de precios del mes de cada fin de mes (la serie mensual viene fechada el día 1)."""
    serie = datos.macro(INFLACION[moneda])
    return _al_fin_de_mes(serie, meses)


def correr(params: Parametros, datos: Datos | None = None, benchmark: dict[str, float] | None = None) -> dict:
    """Backtest completo de la cartera (y del benchmark en el mismo periodo, si se pide)."""
    params.validar()
    datos = datos or Datos()
    activos = [a for a, w in params.pesos.items() if w > 0]
    if benchmark:
        Parametros(pesos=benchmark).validar()
    todos = list(dict.fromkeys(activos + [a for a, w in (benchmark or {}).items() if w > 0] + [EFECTIVO[params.moneda]]))
    primero = date(1998, 1, 31)
    hoy = date.today()
    ultimo_cerrado = fin_de_mes(hoy) if hoy == fin_de_mes(hoy) else date(hoy.year, hoy.month, 1) - timedelta(days=1)
    meses = meses_entre(primero, min(fin_de_mes(params.hasta), ultimo_cerrado) if params.hasta else ultimo_cerrado)
    trm = _al_fin_de_mes(datos.trm(), meses)
    info = {a: activo(a) for a in todos}
    for a in todos:
        if a.startswith("fic:"):
            f = datos.fic().filter(pl.col("id") == a)
            if not f.is_empty():
                ult = f.row(-1, named=True)
                info[a].nombre = f"{_titulo(ult['nombre_fondo'])} · {ult['nombre_entidad']}"
    mat, sin_negocio, avisos = {}, {}, []
    for a in todos:
        nivel, quieto = niveles(a, meses, datos)
        mat[a] = en_moneda(nivel, info[a].moneda, params.moneda, trm)
        if quieto is not None:
            sin_negocio[a] = quieto
    # Periodo común de los activos de la cartera y del benchmark (el efectivo solo se usa si alcanza)
    usados = list(dict.fromkeys(activos + [a for a, w in (benchmark or {}).items() if w > 0]))
    validos = np.all(np.column_stack([~np.isnan(mat[a]) for a in usados]), axis=1)
    if not validos.any():
        raise ValueError("Los activos elegidos no tienen un periodo en común con datos")
    ks = np.flatnonzero(validos)
    inicio, fin = int(ks[0]), int(ks[-1])
    # Un hueco en medio (bono sin tasa un mes) corta el periodo al tramo continuo más reciente
    huecos = np.flatnonzero(~validos[inicio:fin + 1])
    if len(huecos):
        inicio = inicio + int(huecos[-1]) + 1
        avisos.append("Algún activo tiene un hueco en su historia: el periodo empieza después de él.")
    if params.desde:
        pedido = meses.index(fin_de_mes(params.desde)) if fin_de_mes(params.desde) in meses else None
        if pedido is not None and pedido > inicio:
            inicio = pedido
        elif pedido is not None and pedido < inicio:
            avisos.append(f"No todos los activos tienen datos desde {params.desde.isoformat()[:7]}: empieza en {meses[inicio].isoformat()[:7]}.")
    if params.hasta and fin_de_mes(params.hasta) in meses:
        fin = min(fin, meses.index(fin_de_mes(params.hasta)))
    if fin - inicio < 12:
        raise ValueError("El periodo común con datos es de menos de un año: elija otros activos o fechas")
    tramo = slice(inicio, fin + 1)
    m = meses[tramo]
    efectivo = mat[EFECTIVO[params.moneda]][tramo]
    efectivo = None if np.isnan(efectivo).any() else efectivo
    inflacion = _inflacion(params.moneda, m, datos)
    if np.isnan(inflacion).all() or np.isnan(inflacion[-1]):
        inflacion = None
        avisos.append("Sin inflación para el periodo: no se calcula el retorno real.")
    elif np.isnan(inflacion).any():
        desde_ipc = int(np.flatnonzero(~np.isnan(inflacion))[0])
        avisos.append(f"El índice de precios empieza en {m[desde_ipc].isoformat()[:7]}: lo real (retorno y ventanas) se mide desde ahí.")

    def uno(pesos: dict[str, float]) -> tuple[Resultado, dict]:
        ids = [a for a, w in pesos.items() if w > 0]
        p = Parametros(**{**params.__dict__, "pesos": {a: pesos[a] for a in ids}})
        res = simular(np.column_stack([mat[a][tramo] for a in ids]), m, ids, p,
                      np.array([info[a].costo for a in ids]))
        met = metricas(res.indice, m, efectivo, inflacion)
        met.update(rotacion_anual=res.rotacion, costos_anuales=res.costos, rebalanceos=len(res.rebalanceos))
        return res, met

    res, met = uno(params.pesos)
    for a in activos:
        if info[a].aproximado:
            avisos.append(f"{info[a].nombre}: serie aproximada. {info[a].nota}")
        if a in sin_negocio:
            quieto = sin_negocio[a][tramo]
            racha = _racha_maxima(quieto)
            limite = int(config().get("meses_sin_negocio_aviso", 3))
            if racha >= limite:
                avisos.append(f"{info[a].nombre}: hubo {racha} meses seguidos sin negocios; su valor en ese tiempo es el último precio.")
    if efectivo is None:
        avisos.append("Sin tasa del efectivo para todo el periodo: Sharpe y Sortino no se calculan.")
    salida = {
        "periodo": {"desde": m[0].isoformat(), "hasta": m[-1].isoformat(), "meses": len(m) - 1},
        "moneda": params.moneda,
        "activos": [{"id": a, "nombre": info[a].nombre, "peso": params.pesos[a], "costo": info[a].costo,
                     "aproximado": info[a].aproximado, "peso_final": float(res.pesos[-1][k]),
                     "retorno_anual": _anualizado(mat[a][fin] / mat[a][inicio], len(m) - 1)} for k, a in enumerate(res.activos)],
        "serie": [{"fecha": d.isoformat(), "valor": float(v), "indice": float(i), "caida": float(c)}
                  for d, v, i, c in zip(m, res.valor, res.indice, caidas(res.indice))],
        "metricas": met,
        "ventanas": ventanas(res.indice, inflacion),
        "anuales": [{"anio": y, "retorno": r} for y, r in _retornos_anuales(res.indice, m)],
        "aportes": res.aportes,
        "valor_final": float(res.valor[-1]),
        "avisos": avisos,
    }
    if benchmark:
        rb, mb = uno(benchmark)
        salida["benchmark"] = {"pesos": benchmark, "metricas": mb, "ventanas": ventanas(rb.indice, inflacion),
                               "anuales": [{"anio": y, "retorno": r} for y, r in _retornos_anuales(rb.indice, m)]}
        for fila, i, c in zip(salida["serie"], rb.indice, caidas(rb.indice)):
            fila["indice_benchmark"], fila["caida_benchmark"] = float(i), float(c)
    return salida


def _racha_maxima(marcas: np.ndarray) -> int:
    mejor = actual = 0
    for x in marcas:
        actual = actual + 1 if x else 0
        mejor = max(mejor, actual)
    return mejor


def pesos_de_texto(texto: str) -> dict[str, float]:
    """«acciones_co_igual=60,tes_10a=40» (en % o en fracción) → {id: fracción}."""
    pesos = {}
    for parte in [p for p in texto.split(",") if p.strip()]:
        if "=" not in parte:
            raise ValueError(f"Falta el peso en «{parte}» (use activo=peso)")
        k, v = parte.split("=", 1)
        pesos[k.strip()] = float(v.replace("%", "").strip())
    if sum(pesos.values()) > 1.5:
        pesos = {k: v / 100 for k, v in pesos.items()}
    return pesos
