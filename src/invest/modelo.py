"""Modelo de datos del aplicativo: construye las tablas derivadas, la base de consulta y su contrato.

``invest modelo`` hace, en orden:

0. Revisa qué cambió: cada paso del punto 1 guarda la huella (contenido) de lo que lee — tablas,
   archivos de config/, su propio código y, para el calendario, la fecha del día — y de lo que escribe,
   en ``invest-data/.modelo/huellas.json`` (junto a ``modelo.duckdb``, fuera de Git). Un paso cuyos
   insumos y salidas siguen iguales desde su última corrida exitosa se salta; si un paso se recalcula,
   también los que leen sus salidas. ``invest modelo --todo`` recalcula todo.

1. Las tablas derivadas que dependen de dims, factores y ficha: técnico e índices propios
   (``tecnico``), riesgo (``riesgo``), métricas fundamentales y valoración (``metricas``), bandas de
   valoración frente a la propia historia (``bandas``), métricas de
   los bancos con el CUIF (``bancos``, plantilla de bancos), puntajes de
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

import json
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
        SELECT m.*, a.nemotecnico, e.nombre_corto AS emisor_corto, e.nombre AS emisor_nombre,
               v.ev_ebitda, v.deuda_neta_ebitda
        FROM fact_matriz m
        LEFT JOIN dim_activo a USING (especie)
        LEFT JOIN dim_emisor e ON e.emisor_id = m.emisor_id
        LEFT JOIN (SELECT especie, ev_ebitda, deuda_neta_ebitda FROM fact_valoracion
                   WHERE fecha = (SELECT max(fecha) FROM fact_valoracion
                                  WHERE fecha <= (SELECT max(fecha) FROM fact_matriz))) v ON v.especie = m.especie
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


# --------------------------------------------------------------------------- recálculo incremental
# Qué lee y qué escribe cada paso de ``calcular``, en orden. Las tablas son (capa, tabla) como las usan
# ``storage.leer_tabla`` / ``storage.escribir_*`` (las dimensiones de gold, ``dim_*``, viven en
# gold/dims/), ``("usuario", tabla)`` lo que lee ``usuario.leer`` y ``config`` los archivos de config/
# (un patrón con * para carpetas). ``hoy``: el resultado depende de la fecha del día. Una prueba
# (tests/test_modelo_incremental.py) compara estas listas con el código de cada módulo.
PASOS: dict[str, dict] = {
    "tecnico": {
        "modulo": "tecnico",
        "entradas": [("gold", "fact_precio_diario"), ("gold", "fact_liquidez_diaria")],
        "salidas": [("gold", "fact_indice_mercado"), ("gold", "fact_tecnico_diario")],
    },
    "riesgo": {
        "modulo": "riesgo",
        "entradas": [("gold", "fact_tecnico_diario"), ("gold", "fact_indice_mercado"), ("gold", "fact_macro")],
        "salidas": [("gold", "fact_riesgo")],
    },
    "metricas": {
        "modulo": "metricas",
        "entradas": [("gold", "fact_fundamental"), ("gold", "fact_precio_diario")],
        "salidas": [("gold", "fact_metrica_fundamental"), ("gold", "fact_valoracion")],
    },
    "bandas": {
        "modulo": "bandas",
        "entradas": [("gold", "fact_precio_diario"), ("gold", "fact_metrica_fundamental"), ("gold", "fact_split")],
        "config": ["bandas.yaml", "eventos_capital.yaml"],
        "salidas": [("gold", "fact_valoracion_diaria"), ("gold", "fact_quiebre_valoracion"),
                    ("gold", "fact_banda_valoracion_mensual"), ("gold", "fact_banda_valoracion")],
    },
    "bancos": {
        "modulo": "bancos",
        "entradas": [("silver", "fact_cuif_mensual")],
        "config": ["bancos.yaml", "catalogo_fuentes.yaml"],
        "salidas": [("gold", "fact_metrica_banco")],
    },
    "puntajes": {
        "modulo": "puntajes",
        "entradas": [("gold", "fact_valoracion"), ("gold", "dim_activo"), ("gold", "fact_factor_mensual"),
                     ("gold", "fact_tecnico_diario"), ("gold", "fact_riesgo"), ("gold", "fact_metrica_fundamental"),
                     ("gold", "fact_metrica_banco"), ("gold", "fact_puntaje_mensual"), ("gold", "fact_validacion_factor"),
                     ("gold", "fact_banda_valoracion_mensual")],
        "config": ["escala.yaml", "screeners.yaml"],
        "salidas": [("gold", "fact_puntaje"), ("gold", "fact_matriz"), ("gold", "fact_screener_corrida")],
    },
    "calendario": {
        "modulo": "calendario_eventos",
        "entradas": [("gold", "fact_dividendo"), ("gold", "fact_dividendo_web"), ("gold", "dim_activo"),
                     ("gold", "fact_evento_corporativo"), ("gold", "fact_macro")],
        "config": ["calendario_macro.yaml"],
        "hoy": True,  # separa los eventos pasados de los futuros y proyecta el calendario macro
        "salidas": [("gold", "fact_evento_calendario")],
    },
    "cartera": {
        "modulo": "cartera",
        "entradas": [("usuario", "fact_transaccion"), ("usuario", "dim_cartera"), ("usuario", "dim_perfil"),
                     ("usuario", "dim_perfil_asignacion"), ("gold", "fact_precio_diario"), ("gold", "dim_activo"),
                     ("gold", "fact_indice_mercado")],
        "hoy": True,  # sin precios, las posiciones llegan hasta hoy (es un paso de segundos)
        "salidas": [("gold", "fact_posicion"), ("gold", "fact_asignacion"), ("gold", "fact_desempeno")],
    },
    "variables": {
        "modulo": "variables",
        "entradas": [],
        "config": ["variables/*.yaml", "modelo.yaml", "indicadores_macro.yaml"],
        "salidas": [("gold", "dim_variable")],
    },
}

CARPETA_HUELLAS = ".modelo"  # junto a modelo.duckdb; se ignora sola en Git (no va a meta/, que se versiona)
VERSION_HUELLAS = 1


def ruta_huellas() -> Path:
    return data_dir() / CARPETA_HUELLAS / "huellas.json"


def _archivos_tabla(capa: str, tabla: str) -> list[Path]:
    """Los Parquet de una tabla, con las mismas reglas de ruta que storage y usuario."""
    base = data_dir()
    if capa == "usuario":
        archivo = base / "usuario" / f"{tabla}.parquet"
        return [archivo] if archivo.exists() else []
    if capa == "gold" and tabla.startswith("dim_"):
        archivo = base / "gold" / "dims" / f"{tabla}.parquet"
        return [archivo] if archivo.exists() else []
    carpeta = base / capa / tabla
    return sorted(carpeta.glob("**/*.parquet")) if carpeta.exists() else []


def _archivos_config(patron: str) -> list[Path]:
    from invest.config import CONFIG_DIR

    return sorted(CONFIG_DIR.glob(patron)) if "*" in patron else [CONFIG_DIR / patron]


def _modulos_codigo(modulo: str) -> list[Path]:
    """El archivo del módulo y los de invest que importa (un nivel): un cambio de código recalcula."""
    import re

    carpeta = Path(__file__).parent
    fuente = carpeta / f"{modulo}.py"
    nombres = {modulo}
    texto = fuente.read_text(encoding="utf-8")
    for m in re.finditer(r"^from invest(?:\.(\w+))? import ([\w, ]+)", texto, flags=re.M):
        nombres |= {m.group(1)} if m.group(1) else {n.strip() for n in m.group(2).split(",")}
    return sorted(p for p in (carpeta / f"{n}.py" for n in nombres) if p.exists())


class _Huellas:
    """Contenido (sha1) de cada archivo, con caché por tamaño y fecha de modificación: solo se vuelve a
    leer un archivo que cambió, y reescribir un archivo con el mismo contenido no cuenta como cambio."""

    def __init__(self, cache: dict | None = None):
        self.cache: dict[str, list] = dict(cache or {})

    def archivo(self, ruta: Path) -> str:
        import hashlib

        if not ruta.exists():
            return "-"
        st = ruta.stat()
        clave = ruta.as_posix()
        previo = self.cache.get(clave)
        if previo and previo[0] == st.st_size and previo[1] == st.st_mtime_ns:
            return previo[2]
        h = hashlib.sha1()
        with open(ruta, "rb") as f:
            for bloque in iter(lambda: f.read(1 << 20), b""):
                h.update(bloque)
        self.cache[clave] = [st.st_size, st.st_mtime_ns, h.hexdigest()]
        return h.hexdigest()

    def conjunto(self, rutas: list[Path]) -> str:
        import hashlib

        base = data_dir()
        h = hashlib.sha1()
        for r in rutas:
            try:
                nombre = r.relative_to(base).as_posix()
            except ValueError:
                nombre = r.name
            h.update(f"{nombre}={self.archivo(r)}\n".encode())
        return h.hexdigest()

    def entradas(self, nombre: str, hoy: str) -> str:
        p = PASOS[nombre]
        partes = [f"v{VERSION_HUELLAS}", "codigo:" + self.conjunto(_modulos_codigo(p["modulo"]))]
        for capa, tabla in p["entradas"]:
            partes.append(f"{capa}/{tabla}:" + self.conjunto(_archivos_tabla(capa, tabla)))
        for patron in p.get("config", []):
            partes.append(f"config/{patron}:" + self.conjunto(_archivos_config(patron)))
        if p.get("hoy"):
            partes.append("hoy:" + hoy)
        return "|".join(partes)

    def salidas(self, nombre: str) -> str:
        return "|".join(f"{capa}/{tabla}:" + self.conjunto(_archivos_tabla(capa, tabla))
                        for capa, tabla in PASOS[nombre]["salidas"])


def _leer_huellas() -> dict:
    archivo = ruta_huellas()
    try:
        d = json.loads(archivo.read_text(encoding="utf-8"))
        return d if d.get("version") == VERSION_HUELLAS else {}
    except (OSError, ValueError):
        return {}  # primera corrida o archivo dañado: se recalcula todo


def _guardar_huellas(pasos: dict, cache: dict) -> None:
    archivo = ruta_huellas()
    archivo.parent.mkdir(parents=True, exist_ok=True)
    ignorar = archivo.parent / ".gitignore"
    if not ignorar.exists():
        ignorar.write_text("# Huellas del recálculo incremental de invest modelo: propias de este computador\n*\n",
                           encoding="utf-8")
    base = data_dir().as_posix() + "/"
    cache = {k: v for k, v in cache.items() if not k.startswith(base) or Path(k).exists()}
    tmp = archivo.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": VERSION_HUELLAS, "pasos": pasos, "archivos": cache}, ensure_ascii=False,
                              indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(archivo)


def depende_de(nombre: str) -> set[str]:
    """Pasos anteriores cuyas salidas lee ``nombre``."""
    orden = list(PASOS)
    entradas = set(PASOS[nombre]["entradas"])
    return {otro for otro in orden[:orden.index(nombre)] if entradas & set(PASOS[otro]["salidas"])}


def calcular(todo: bool = True, informar=None, hoy=None) -> dict[str, dict]:
    """Recalcula las tablas derivadas en orden.

    Con ``todo=False`` salta los pasos cuyos insumos (tablas que lee, archivos de config/, su código y,
    si aplica, la fecha del día) no cambiaron desde su última corrida exitosa y cuyas salidas siguen
    como las dejó. Si un paso se recalcula, también los que leen sus salidas. Los saltados quedan en el
    resumen como ``{"saltado": True}``. ``informar(nombre, resultado)`` se llama al terminar cada paso.
    ``hoy`` (fecha) solo cambia la huella de los pasos que dependen del día; sirve para las pruebas.
    """
    import importlib
    from datetime import date

    previas = _leer_huellas()  # con todo=True también: los pasos que no alcance a correr conservan su huella
    huellas = _Huellas(previas.get("archivos"))
    estado = dict(previas.get("pasos") or {})
    hoy = (hoy or date.today()).isoformat()
    resumen: dict[str, dict] = {}
    recalculados: set[str] = set()
    for nombre, paso in PASOS.items():
        entradas = huellas.entradas(nombre, hoy)
        anterior = estado.get(nombre) or {}
        arrastre = depende_de(nombre) & recalculados
        if (not todo and not arrastre and anterior.get("entradas") == entradas
                and anterior.get("salidas") == huellas.salidas(nombre)):
            resumen[nombre] = {"saltado": True}
        else:
            estado.pop(nombre, None)
            _guardar_huellas(estado, huellas.cache)  # si el paso falla, la próxima corrida lo repite
            modulo = importlib.import_module(f"invest.{paso['modulo']}")
            resumen[nombre] = modulo.construir()
            recalculados.add(nombre)
            estado[nombre] = {"entradas": entradas, "salidas": huellas.salidas(nombre)}
            _guardar_huellas(estado, huellas.cache)
        if informar:
            informar(nombre, resumen[nombre])
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


def construir(recalcular: bool = True, todo: bool = False, informar=None) -> dict:
    """Tablas derivadas (solo los pasos con insumos nuevos, o todos con ``todo=True``), base y contrato."""
    resumen = calcular(todo=todo, informar=informar) if recalcular else {}
    # La salud de datos va en la base: se evalúa aquí (segundos) para que la página no quede una corrida atrás
    try:
        from invest import salud

        salud.evaluar()
    except Exception as e:  # noqa: BLE001 — la salud es informativa; la base se construye igual
        resumen["salud"] = {"error": f"{type(e).__name__}: {e}"}
    destino, vistas = construir_duckdb()
    problemas = verificar()
    return {"calculo": resumen, "saltados": [n for n, r in resumen.items() if r.get("saltado")],
            "duckdb": str(destino), "vistas": len(vistas), "problemas": problemas}

