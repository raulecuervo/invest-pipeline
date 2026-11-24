"""Modelo de datos del aplicativo: construye las tablas derivadas, la base de consulta y su contrato.

``invest modelo`` hace, en orden:

1. Las tablas derivadas que dependen de dims, factores y ficha: técnico e índices propios
   (``tecnico``), riesgo (``riesgo``), métricas fundamentales y valoración (``metricas``), puntajes de
   1 a 10, matriz y screeners (``puntajes``), calendario (``calendario_eventos``), carteras
   (``cartera``, a partir de lo que el usuario ingresó en ``usuario/``) y el catálogo de variables
   para el usuario (``variables``, de config/variables/).
2. ``invest-data/modelo.duckdb``: una vista por tabla de config/modelo.yaml sobre sus Parquet (gold,
   silver y usuario), más vistas de conveniencia para el aplicativo (``v_precio_ajustado``,
   ``v_ficha_actual``, ``v_mapa_mercado``). El archivo no se versiona: guarda rutas de este computador
   y se rehace en segundos. El aplicativo solo lee de aquí; escribe en ``usuario/`` con
   ``invest.usuario.guardar``.
3. La verificación del contrato: cada tabla del catálogo existe, trae sus columnas con un tipo
   compatible y sus claves no se repiten. Devuelve la lista de problemas.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl

from invest import usuario
from invest.config import cargar_yaml, data_dir

VISTAS = {
    "v_precio_ajustado": """
        SELECT r.*, r.indice_rt * last_value(r.precio_promedio) OVER w / last_value(r.indice_rt) OVER w AS precio_ajustado
        FROM fact_retorno_diario r
        WINDOW w AS (PARTITION BY especie ORDER BY fecha ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)""",
    "v_ficha_actual": """
        SELECT m.*, a.nemotecnico, e.nombre_corto AS emisor_corto, e.nombre AS emisor_nombre
        FROM fact_matriz m
        LEFT JOIN dim_activo a USING (especie)
        LEFT JOIN dim_emisor e ON e.emisor_id = m.emisor_id
        WHERE m.fecha = (SELECT max(fecha) FROM fact_matriz)""",
    "v_mapa_mercado": """
        WITH t AS (
            SELECT especie, retorno_1m, retorno_12m, caida_desde_max_52s,
                   row_number() OVER (PARTITION BY especie ORDER BY fecha DESC) AS n
            FROM fact_tecnico_diario)
        SELECT f.especie, f.nemotecnico, f.emisor_corto, f.sector_sfc, f.en_universo, f.capitalizacion,
               f.capitalizacion_usd, f.puntaje_global, f.cuadrante, t.retorno_1m, t.retorno_12m, t.caida_desde_max_52s
        FROM v_ficha_actual f LEFT JOIN t ON t.especie = f.especie AND t.n = 1""",
}

COMPATIBLES = {
    "String": ("String", "Utf8", "Null"), "Date": ("Date", "Null"), "Boolean": ("Boolean", "Null"),
    "Float64": ("Float64", "Float32", "Int64", "Int32", "UInt32", "Null"),
    "Int64": ("Int64", "Int32", "UInt32", "Int8", "Int16", "UInt64", "Null"),
    "Int32": ("Int32", "Int64", "UInt32", "Null"), "UInt32": ("UInt32", "Int64", "Int32", "Null"),
}


def catalogo() -> dict:
    return cargar_yaml("modelo.yaml")["tablas"]


def _ruta(definicion: dict) -> Path:
    return data_dir() / definicion["capa"] / definicion["ruta"]


def _archivos(definicion: dict) -> list[Path]:
    ruta = _ruta(definicion)
    if ruta.suffix == ".parquet":
        return [ruta] if ruta.exists() else []
    return sorted(ruta.glob("**/*.parquet")) if ruta.exists() else []


def leer(nombre: str) -> pl.DataFrame | None:
    """Lee una tabla del catálogo por su nombre."""
    d = catalogo()[nombre]
    archivos = _archivos(d)
    if not archivos:
        return None
    return pl.concat([pl.read_parquet(a) for a in archivos], how="diagonal_relaxed")


def calcular() -> dict[str, dict]:
    from invest import calendario_eventos, cartera, metricas, puntajes, riesgo, tecnico, variables

    resumen = {}
    for nombre, modulo in (("tecnico", tecnico), ("riesgo", riesgo), ("metricas", metricas),
                           ("puntajes", puntajes), ("calendario", calendario_eventos), ("cartera", cartera),
                           ("variables", variables)):
        resumen[nombre] = modulo.construir()
    return resumen


def construir_duckdb(destino: Path | None = None) -> tuple[Path, list[str]]:
    destino = destino or data_dir() / "modelo.duckdb"
    tmp = destino.with_suffix(".duckdb.tmp")
    if tmp.exists():
        tmp.unlink()
    creadas = []
    with duckdb.connect(str(tmp)) as con:
        for nombre, d in catalogo().items():
            archivos = _archivos(d)
            if not archivos:
                continue
            lista = ", ".join("'" + a.as_posix().replace("'", "''") + "'" for a in archivos)
            con.execute(f"CREATE VIEW {nombre} AS SELECT * FROM read_parquet([{lista}], union_by_name = true, "
                        "hive_partitioning = false)")
            creadas.append(nombre)
        if not any(usuario.carpeta().glob("*.parquet")):
            usuario.plantillas()  # tablas vacías para que el aplicativo encuentre las vistas
        for tabla in usuario.TABLAS:
            archivo = usuario.carpeta() / f"{tabla}.parquet"
            if archivo.exists():
                con.execute(f"CREATE VIEW {tabla} AS SELECT * FROM read_parquet('{archivo.as_posix()}')")
                creadas.append(tabla)
        for nombre, sql in VISTAS.items():
            try:
                con.execute(f"CREATE VIEW {nombre} AS {sql}")
                creadas.append(nombre)
            except duckdb.Error:
                pass  # falta alguna tabla de la que depende (p. ej. antes de la primera corrida)
    try:
        tmp.replace(destino)
    except PermissionError:
        # El aplicativo la tiene abierta (Windows no deja reemplazarla): queda la anterior y se
        # vuelve a intentar en la próxima corrida
        tmp.unlink(missing_ok=True)
        return destino, []
    return destino, creadas


def verificar() -> list[str]:
    problemas = []
    for nombre, d in catalogo().items():
        df = leer(nombre)
        if df is None:
            if d["capa"] == "gold" and not nombre.startswith("fact_posicion") and nombre not in (
                    "fact_asignacion", "fact_desempeno"):
                problemas.append(f"{nombre}: no existe")
            continue
        for col, tipo in (d.get("columnas") or {}).items():
            if col not in df.columns:
                problemas.append(f"{nombre}: falta la columna {col}")
                continue
            real = str(df.schema[col]).split("(")[0]
            if real not in COMPATIBLES.get(tipo, (tipo, "Null")):
                problemas.append(f"{nombre}.{col}: tipo {real}, el catálogo dice {tipo}")
        claves = d.get("claves") or []
        if claves and all(c in df.columns for c in claves):
            repetidas = df.group_by(claves).len().filter(pl.col("len") > 1).height
            if repetidas:
                problemas.append(f"{nombre}: {repetidas} claves repetidas en {claves}")
    return problemas


def resumen_tablas() -> pl.DataFrame:
    filas = []
    for nombre, d in catalogo().items():
        archivos = _archivos(d)
        filas.append({"tabla": nombre, "capa": d["capa"], "archivos": len(archivos),
                      "megabytes": round(sum(a.stat().st_size for a in archivos) / 1e6, 2), "pagina": d.get("pagina")})
    return pl.DataFrame(filas)


def construir(recalcular: bool = True) -> dict:
    resumen = calcular() if recalcular else {}
    destino, vistas = construir_duckdb()
    problemas = verificar()
    return {"calculo": resumen, "duckdb": str(destino), "vistas": len(vistas), "problemas": problemas}

