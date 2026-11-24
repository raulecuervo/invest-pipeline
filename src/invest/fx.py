"""Tasas a pesos por día para pagos en otras monedas (dividendos en dólares o en dólares canadienses).

- USD: la TRM del día (silver/fact_fx_diaria).
- CAD: TRM / (dólares canadienses por dólar, serie DEXCAUS de FRED en silver/fact_serie_macro), con la
  última cotización de FRED de los 7 días anteriores (FRED no publica los festivos de EE. UU.).
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from invest import storage


def _trm() -> dict[date, float]:
    fx = storage.leer_tabla("silver", "fact_fx_diaria")
    if fx is None:
        return {}
    usd = fx.filter((pl.col("moneda_origen") == "USD") & (pl.col("moneda_destino") == "COP"))
    return dict(usd.select("fecha", "tasa").iter_rows())


def _fred(serie_id: str) -> dict[date, float]:
    s = storage.leer_tabla("silver", "fact_serie_macro")
    if s is None or s.is_empty():
        return {}
    return dict(s.filter((pl.col("serie_id") == serie_id) & pl.col("valor").is_not_null())
                .select("fecha", "valor").iter_rows())


def tasas_a_cop(moneda: str, trm: dict[date, float] | None = None,
                cad_por_usd: dict[date, float] | None = None) -> dict[date, float]:
    """Pesos por unidad de ``moneda`` en cada fecha con TRM (vacío si la moneda no se sabe convertir)."""
    moneda = (moneda or "COP").upper()
    trm = _trm() if trm is None else trm
    if moneda == "USD":
        return trm
    if moneda == "CAD":
        cad = _fred("DEXCAUS") if cad_por_usd is None else cad_por_usd
        salida = {}
        for f, t in trm.items():
            for atras in range(8):
                c = cad.get(f - timedelta(days=atras))
                if c:
                    salida[f] = t / c
                    break
        return salida
    return {}
