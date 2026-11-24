"""Catálogo de variables para el usuario (``gold/dims/dim_variable``), desde config/variables/*.yaml.

Cada variable tiene un archivo con su nombre, categoría, nivel, formato, dirección, fórmula, de qué
tabla y columna sale, y la explicación breve del centro de aprendizaje (qué es, qué hace, para qué
sirve, cómo leerla), más sus variables relacionadas. Es la única fuente: de aquí leen el aplicativo
(ícono de información y página Saber más) y las pruebas que verifican que la variable exista en el
modelo. Los textos se generaron como borrador; ``revisado: true`` indica que Esteban los leyó.
"""

from __future__ import annotations

import polars as pl
import yaml

from invest import storage
from invest.bancos import METRICAS as METRICAS_BANCO
from invest.config import CONFIG_DIR, cargar_yaml

CAMPOS = ["id", "nombre", "categoria", "nivel", "formato", "direccion", "tabla", "columna", "metrica", "serie_id",
          "formula", "que_es", "que_hace", "para_que_sirve", "como_leer", "relacionadas", "revisado", "version"]


def cargar() -> list[dict]:
    salida = []
    for archivo in sorted((CONFIG_DIR / "variables").glob("*.yaml")):
        d = yaml.safe_load(archivo.read_text(encoding="utf-8"))
        d["_archivo"] = archivo.name
        salida.append(d)
    return salida


def problemas(variables: list[dict] | None = None) -> list[str]:
    """Verifica que cada variable apunte a algo que existe en el modelo."""
    variables = variables if variables is not None else cargar()
    tablas = cargar_yaml("modelo.yaml")["tablas"]
    series = set(cargar_yaml("indicadores_macro.yaml").get("indicadores", {})) | set(
        cargar_yaml("indicadores_macro.yaml").get("derivados", {}))
    metricas_conocidas = {"vpa", "upa_12m", "roe_12m", "dividendo_accion", "payout", "crecimiento_upa_12m",
                          "ingresos_12m", "crecimiento_ingresos_12m", "utilidad_controladora_12m",
                          "roe_consolidado_12m", "margen_neto_12m", "apalancamiento", "efectivo_activos",
                          "utilidad_operacional_12m", "ebitda_12m", "margen_bruto_12m", "margen_operacional_12m",
                          "margen_ebitda_12m", "deuda_financiera", "deuda_neta", "deuda_neta_ebitda",
                          "interes_minoritario"}
    ids = {v["id"] for v in variables}
    salida = []
    for v in variables:
        if v["id"] + ".yaml" != v["_archivo"]:
            salida.append(f"{v['_archivo']}: el id no coincide con el nombre del archivo")
        faltan = [c for c in ("nombre", "que_es", "que_hace", "para_que_sirve", "como_leer", "formula") if not v.get(c)]
        if faltan:
            salida.append(f"{v['id']}: faltan {faltan}")
        t = tablas.get(v.get("tabla"))
        if t is None:
            salida.append(f"{v['id']}: la tabla {v.get('tabla')} no está en config/modelo.yaml")
        elif v.get("columna") not in (t.get("columnas") or {}):
            salida.append(f"{v['id']}: {v['tabla']} no tiene la columna {v.get('columna')}")
        if v.get("tabla") == "fact_macro" and v.get("serie_id") not in series:
            salida.append(f"{v['id']}: la serie {v.get('serie_id')} no está en config/indicadores_macro.yaml")
        if v.get("tabla") == "fact_metrica_fundamental" and v.get("metrica") not in metricas_conocidas:
            salida.append(f"{v['id']}: métrica desconocida {v.get('metrica')}")
        if v.get("tabla") == "fact_metrica_banco" and v.get("metrica") not in METRICAS_BANCO:
            salida.append(f"{v['id']}: métrica de banco desconocida {v.get('metrica')}")
        for r in v.get("relacionadas") or []:
            if r not in ids:
                salida.append(f"{v['id']}: la relacionada {r} no tiene archivo")
    return salida


def dim_variable() -> pl.DataFrame:
    filas = []
    for v in cargar():
        fila = {c: v.get(c) for c in CAMPOS}
        fila["relacionadas"] = ", ".join(v.get("relacionadas") or [])
        fila["revisado"] = bool(v.get("revisado"))
        filas.append(fila)
    return pl.DataFrame(filas, infer_schema_length=None)


def construir() -> dict:
    df = dim_variable()
    if df.is_empty():
        return {}
    storage.escribir_completa("gold", "dim_variable", df)
    return {"variables": df.height, "revisadas": int(df.get_column("revisado").sum()), "problemas": len(problemas())}
