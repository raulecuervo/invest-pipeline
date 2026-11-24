"""Calendarios bursátiles por bolsa (exchange_calendars), con respaldo por días de semana.

exchange_calendars cubre un rango limitado de años; fuera de ese rango se usa lunes a viernes,
que es suficiente para fechas históricas antiguas.
"""

from __future__ import annotations

import warnings
from datetime import date, timedelta
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd


@lru_cache(maxsize=None)
def _calendario(mic: str):
    # exchange_calendars usa una función de pandas que pandas 3.1 marca como obsoleta; el aviso es de
    # la librería, no de este código, y no cambia el resultado
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Series.values returning an ndarray")
        return xcals.get_calendar(mic)


@lru_cache(maxsize=None)
def _sesiones(mic: str) -> frozenset[date]:
    cal = _calendario(mic)
    return frozenset(d.date() for d in cal.sessions)


def limites(mic: str) -> tuple[date, date]:
    cal = _calendario(mic)
    return cal.first_session.date(), cal.last_session.date()


def es_habil(dia: date, mic: str = "XBOG") -> bool:
    primero, ultimo = limites(mic)
    if primero <= dia <= ultimo:
        return dia in _sesiones(mic)
    return dia.weekday() < 5


def dia_habil_anterior(dia: date, mic: str = "XBOG") -> date:
    """Último día hábil estrictamente anterior a ``dia``."""
    d = dia - timedelta(days=1)
    for _ in range(30):
        if es_habil(d, mic):
            return d
        d -= timedelta(days=1)
    raise ValueError(f"No se encontró día hábil anterior a {dia} en {mic}")


def sesiones_entre(desde: date, hasta: date, mic: str = "XBOG") -> list[date]:
    dias = pd.date_range(desde, hasta, freq="D")
    return [d.date() for d in dias if es_habil(d.date(), mic)]


def dia_habil_siguiente_o_mismo(dia: date, mic: str = "XBOG") -> date:
    """``dia`` si es hábil; si no, el siguiente día hábil."""
    d = dia
    for _ in range(30):
        if es_habil(d, mic):
            return d
        d += timedelta(days=1)
    raise ValueError(f"No se encontró día hábil desde {dia} en {mic}")


def restar_dias_habiles(dia: date, n: int, mic: str = "XBOG") -> date:
    """El día hábil que está ``n`` días hábiles antes de ``dia``."""
    d = dia
    for _ in range(n):
        d = dia_habil_anterior(d, mic)
    return d
