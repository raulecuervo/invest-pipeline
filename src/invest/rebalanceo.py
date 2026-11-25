"""Calculadora de rebalanceo: cuánto mover en cada clase de activo para volver a la asignación objetivo del
perfil de una cartera, con un aporte o retiro opcional.

Es aritmética sobre el objetivo que el usuario fijó en su perfil (``dim_perfil_asignacion``), no una
recomendación: no elige acciones nuevas ni opina sobre ninguna. Dentro de una clase, el monto se reparte
entre las posiciones que ya tiene en proporción a su valor (conserva su mezcla interna); si la clase no
tiene posiciones, el monto queda "por asignar" en la clase.

Modos:

- ``completo``: lleva cada clase a su peso objetivo sobre (valor actual + aporte); puede vender.
- ``solo_aportes``: no vende. El efectivo que sobra frente a su objetivo (con el aporte) va a las clases
  por debajo del suyo, en proporción a lo que les falta; lo que no se necesita queda en efectivo. Un
  retiro sale primero del efectivo y, si no alcanza, de las clases por encima de su objetivo.

El efectivo es una clase más: lo que se compra sale de él (y del aporte) y lo que se vende vuelve a él.
Las cantidades se redondean a acciones enteras hacia abajo y el residuo queda en efectivo. Los costos son
una estimación con la comisión que se indique (el GMF y la retención dependen de cómo se mueva el dinero).
"""
from __future__ import annotations

import math

EFECTIVO = "EFECTIVO"


def _pct(x: float, decimales: int = 1) -> str:
    return f"{x * 100:.{decimales}f}".replace(".", ",") + " %"


def _por_clase(posiciones: list[dict]) -> dict[str, float]:
    total: dict[str, float] = {}
    for p in posiciones:
        total[p["clase_activo_id"]] = total.get(p["clase_activo_id"], 0.0) + float(p["valor_cop"] or 0.0)
    return total


def _montos_completo(actual: dict[str, float], objetivo: dict[str, float], nuevo_total: float) -> dict[str, float]:
    clases = set(actual) | set(objetivo)
    return {c: objetivo.get(c, 0.0) * nuevo_total - actual.get(c, 0.0) for c in clases}


def _montos_solo_aportes(actual: dict[str, float], objetivo: dict[str, float], aporte: float) -> dict[str, float]:
    """Sin vender: el efectivo que sobra frente a su objetivo (incluido el aporte) va a las clases por debajo
    del suyo, en proporción a lo que les falta. Un retiro sale primero del efectivo y, si no alcanza, de las
    clases por encima de su objetivo (y al final de todas, en proporción a su valor)."""
    nuevo_total = sum(actual.values()) + aporte
    meta = {c: objetivo.get(c, 0.0) * nuevo_total for c in set(actual) | set(objetivo) | {EFECTIVO}}
    montos = {c: 0.0 for c in meta}
    efectivo = actual.get(EFECTIVO, 0.0) + aporte
    if efectivo >= 0:
        disponible = max(efectivo - meta[EFECTIVO], 0.0)
        faltante = {c: max(meta[c] - actual.get(c, 0.0), 0.0) for c in meta if c != EFECTIVO}
        total_faltante = sum(faltante.values())
        usar = min(disponible, total_faltante)
        for c, f in faltante.items():
            if total_faltante > 0:
                montos[c] = usar * f / total_faltante
    else:
        # El retiro es mayor que el efectivo: lo que falta sale de las demás clases
        falta = -efectivo
        exceso = {c: max(actual.get(c, 0.0) - meta[c], 0.0) for c in meta if c != EFECTIVO}
        total_exceso = sum(exceso.values())
        sacar = min(falta, total_exceso)
        for c, e in exceso.items():
            if total_exceso > 0:
                montos[c] -= sacar * e / total_exceso
        resto = falta - sacar
        if resto > 1e-9:
            base = sum(v for c, v in actual.items() if c != EFECTIVO) or 1.0
            for c in exceso:
                montos[c] -= resto * actual.get(c, 0.0) / base
    montos[EFECTIVO] = aporte - sum(v for c, v in montos.items() if c != EFECTIVO)
    return montos


def calcular(posiciones: list[dict], objetivo: dict[str, float], aporte: float = 0.0, modo: str = "completo",
             comision: float = 0.0, maximo_por_emisor: float | None = None) -> dict:
    """``posiciones``: especie, clase_activo_id, valor_cop, precio, cantidad y, si se tiene, emisor y nombre.
    ``objetivo``: clase → peso (debe sumar 1; se normaliza si no). Devuelve clases, operaciones y avisos."""
    if modo not in ("completo", "solo_aportes"):
        raise ValueError(f"modo desconocido: {modo}")
    avisos: list[str] = []
    suma = sum(objetivo.values())
    if suma <= 0:
        raise ValueError("el perfil no tiene asignación objetivo")
    if abs(suma - 1) > 0.005:
        avisos.append(f"Los pesos objetivo suman {_pct(suma)}; se normalizaron a 100 %.")
    objetivo = {c: w / suma for c, w in objetivo.items()}
    actual = _por_clase(posiciones)
    total = sum(actual.values())
    if total + aporte <= 0:
        raise ValueError("el retiro es mayor que el valor de la cartera")
    nuevo_total = total + aporte
    montos = (_montos_completo(actual, objetivo, nuevo_total) if modo == "completo"
              else _montos_solo_aportes(actual, objetivo, aporte))

    # Reparto dentro de cada clase, en proporción al valor de cada posición, en acciones enteras
    operaciones, por_asignar = [], {}
    ejecutado = {c: 0.0 for c in montos}
    for clase, monto in montos.items():
        if clase == EFECTIVO or abs(monto) < 1:
            continue
        de_clase = [p for p in posiciones if p["clase_activo_id"] == clase and (p.get("precio") or 0) > 0]
        base = sum(float(p["valor_cop"] or 0) for p in de_clase)
        if not de_clase or base <= 0:
            por_asignar[clase] = monto
            ejecutado[clase] = monto
            continue
        # Acciones enteras hacia abajo; luego el residuo compra (o vende) una acción más donde la parte
        # redondeada perdió más, mientras alcance (método del mayor residuo)
        partes = []
        for p in de_clase:
            parte = abs(monto) * float(p["valor_cop"]) / base
            precio = float(p["precio"])
            tope = int(p.get("cantidad") or 0) if monto < 0 else None
            cantidad = math.floor(parte / precio)
            if tope is not None:
                cantidad = min(cantidad, tope)
            partes.append([p, precio, cantidad, parte / precio - cantidad, tope])
        residuo = abs(monto) - sum(c * pr for _, pr, c, _, _ in partes)
        for fila in sorted(partes, key=lambda x: -x[3]):
            _, precio, cantidad, _, tope = fila
            if precio <= residuo + 1e-9 and (tope is None or cantidad < tope):
                fila[2] += 1
                residuo -= precio
        for p, precio, cantidad, _, _ in partes:
            if cantidad == 0:
                continue
            valor = cantidad * precio * (1 if monto > 0 else -1)
            ejecutado[clase] += valor
            operaciones.append({
                "especie": p["especie"], "nombre": p.get("nombre"), "clase_activo_id": clase,
                "operacion": "aumentar" if monto > 0 else "reducir", "cantidad": cantidad, "precio": precio,
                "monto": valor, "comision": abs(valor) * comision,
            })
    costo = sum(o["comision"] for o in operaciones)
    # El efectivo recibe el aporte, entrega lo comprado, recibe lo vendido y paga la comisión
    ejecutado[EFECTIVO] = aporte - sum(v for c, v in ejecutado.items() if c != EFECTIVO) - costo

    clases = []
    for c in sorted(set(actual) | set(objetivo)):
        despues = actual.get(c, 0.0) + ejecutado.get(c, 0.0)
        clases.append({
            "clase_activo_id": c, "valor_actual": actual.get(c, 0.0), "peso_actual": actual.get(c, 0.0) / total if total else None,
            "peso_objetivo": objetivo.get(c, 0.0), "monto": ejecutado.get(c, 0.0),
            "valor_despues": despues, "peso_despues": despues / (nuevo_total - costo) if nuevo_total - costo else None,
        })
    if por_asignar:
        avisos.append("Sin posiciones en " + ", ".join(por_asignar) + ": el monto queda por asignar dentro de la clase.")
    if any(c["valor_despues"] < -1 for c in clases if c["clase_activo_id"] == EFECTIVO):
        avisos.append("El efectivo quedaría negativo: el aporte no alcanza para las compras de este modo.")
    if maximo_por_emisor:
        por_emisor: dict[str, float] = {}
        cambio = {o["especie"]: o["monto"] for o in operaciones}
        for p in posiciones:
            if p["clase_activo_id"] == EFECTIVO:
                continue
            clave = p.get("emisor") or p["especie"]
            por_emisor[clave] = por_emisor.get(clave, 0.0) + float(p["valor_cop"] or 0) + cambio.get(p["especie"], 0.0)
        for emisor, v in sorted(por_emisor.items()):
            if nuevo_total and v / nuevo_total > maximo_por_emisor + 1e-9:
                avisos.append(f"{emisor} quedaría en {_pct(v / nuevo_total)} de la cartera, por encima del máximo "
                              f"por emisor del perfil ({_pct(maximo_por_emisor, 0)}).")
    return {"modo": modo, "aporte": aporte, "valor_actual": total, "valor_despues": nuevo_total - costo,
            "costo_estimado": costo, "clases": clases, "operaciones": operaciones, "avisos": avisos}
