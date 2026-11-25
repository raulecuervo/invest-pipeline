"""Datos que ingresa el usuario: perfiles, carteras, transacciones, tesis y parámetros.

Viven en ``invest-data/usuario/<tabla>.parquet`` (repositorio privado, nunca en el público). La app
los escribe con ``guardar``, que valida columnas, tipos, valores permitidos y claves antes de tocar el
archivo; si algo no cumple, no escribe nada y devuelve los problemas. ``plantillas`` crea las tablas
vacías y un CSV de ejemplo por tabla en ``usuario/plantillas/`` para cargar desde Excel.

Tablas (grano → claves):
- ``dim_perfil``: perfil-versión. Objetivo, horizonte, moneda base, tolerancia al riesgo, meta de
  retorno real, liquidez de reserva, benchmark, vigencia. Editar un perfil = nueva versión.
- ``dim_perfil_asignacion``: perfil-versión × clase × país × moneda. Peso objetivo y bandas.
- ``dim_cartera``: cartera. Perfil asociado, tipo (real o hipotética), moneda base, intermediario.
- ``fact_transaccion``: una fila por movimiento (compra, venta, dividendo, dividendo_acciones, aporte,
  retiro, comision, impuesto). Los montos van en la moneda de la transacción con la TRM aplicada.
- ``dim_tesis``: cartera × especie. Tesis, catalizador, precio objetivo, condición de invalidación,
  fecha de revisión.
- ``dim_parametro_tributario``: regla × vigencia. Vacía a propósito: se llena con un contador (las
  reglas de 2026 están en movimiento y no se adivinan).
- ``dim_costo_intermediario``: intermediario × producto. Comisión, mínimo, custodia.
- ``dim_alerta``: alertas propias (precio, P/VL, z del P/VL o rendimiento por dividendo de una acción frente
  a un umbral). Las alertas automáticas (cartera, tesis, dividendos, valoración) no necesitan reglas.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from invest.config import data_dir

TABLAS: dict[str, dict] = {
    "dim_perfil": {
        "claves": ["perfil_id", "version"],
        "columnas": {
            "perfil_id": pl.Utf8, "version": pl.Int64, "nombre": pl.Utf8, "objetivo": pl.Utf8,
            "horizonte_anios": pl.Float64, "moneda_base": pl.Utf8, "perdida_maxima_aceptable": pl.Float64,
            "volatilidad_objetivo": pl.Float64, "meta_retorno_real": pl.Float64, "liquidez_reserva": pl.Float64,
            "benchmark": pl.Utf8, "maximo_por_emisor": pl.Float64, "excluidos": pl.Utf8, "notas": pl.Utf8,
            "vigente_desde": pl.Date, "vigente_hasta": pl.Date,
        },
        "obligatorias": ["perfil_id", "version", "nombre", "objetivo", "moneda_base", "vigente_desde"],
        "permitidos": {"objetivo": ["crecimiento", "renta", "preservacion"], "moneda_base": ["COP", "USD"]},
    },
    "dim_perfil_asignacion": {
        "claves": ["perfil_id", "version", "clase_activo_id", "pais_id", "moneda_id"],
        "columnas": {
            "perfil_id": pl.Utf8, "version": pl.Int64, "clase_activo_id": pl.Utf8, "pais_id": pl.Utf8,
            "moneda_id": pl.Utf8, "peso_objetivo": pl.Float64, "banda_minima": pl.Float64, "banda_maxima": pl.Float64,
        },
        "obligatorias": ["perfil_id", "version", "clase_activo_id", "peso_objetivo"],
        "permitidos": {},
    },
    "dim_cartera": {
        "claves": ["cartera_id"],
        "columnas": {
            "cartera_id": pl.Utf8, "nombre": pl.Utf8, "perfil_id": pl.Utf8, "tipo": pl.Utf8, "moneda_base": pl.Utf8,
            "intermediario": pl.Utf8, "creada": pl.Date, "copiada_de": pl.Utf8, "notas": pl.Utf8,
        },
        "obligatorias": ["cartera_id", "nombre", "tipo", "moneda_base"],
        "permitidos": {"tipo": ["real", "hipotetica"], "moneda_base": ["COP", "USD"]},
    },
    "fact_transaccion": {
        "claves": ["transaccion_id"],
        "columnas": {
            "transaccion_id": pl.Utf8, "cartera_id": pl.Utf8, "fecha_operacion": pl.Date, "fecha_liquidacion": pl.Date,
            "tipo": pl.Utf8, "especie": pl.Utf8, "cantidad": pl.Float64, "precio": pl.Float64, "monto": pl.Float64,
            "comision": pl.Float64, "gmf": pl.Float64, "retencion": pl.Float64, "moneda": pl.Utf8, "trm": pl.Float64,
            "notas": pl.Utf8,
        },
        "obligatorias": ["transaccion_id", "cartera_id", "fecha_operacion", "tipo", "moneda"],
        "permitidos": {"tipo": ["compra", "venta", "dividendo", "dividendo_acciones", "aporte", "retiro", "comision",
                                "impuesto"], "moneda": ["COP", "USD"]},
    },
    "dim_tesis": {
        "claves": ["cartera_id", "especie"],
        "columnas": {
            "cartera_id": pl.Utf8, "especie": pl.Utf8, "tesis": pl.Utf8, "catalizador": pl.Utf8,
            "precio_objetivo": pl.Float64, "condicion_invalidacion": pl.Utf8, "fecha_revision": pl.Date,
            "expectativa_retorno": pl.Float64, "horizonte_meses": pl.Int64, "creada": pl.Date, "actualizada": pl.Date,
        },
        "obligatorias": ["cartera_id", "especie", "tesis"],
        "permitidos": {},
    },
    "dim_parametro_tributario": {
        "claves": ["regla_id", "vigente_desde"],
        "columnas": {
            "regla_id": pl.Utf8, "descripcion": pl.Utf8, "tipo_rendimiento": pl.Utf8, "tarifa": pl.Float64,
            "base_minima": pl.Float64, "vigente_desde": pl.Date, "vigente_hasta": pl.Date, "fuente": pl.Utf8,
            "validado_por": pl.Utf8,
        },
        "obligatorias": ["regla_id", "tarifa", "vigente_desde", "fuente"],
        "permitidos": {},
    },
    "dim_alerta": {
        "claves": ["alerta_id"],
        "columnas": {
            "alerta_id": pl.Utf8, "especie": pl.Utf8, "condicion": pl.Utf8, "umbral": pl.Float64, "activa": pl.Utf8,
            "nota": pl.Utf8, "creada": pl.Date,
        },
        "obligatorias": ["alerta_id", "especie", "condicion", "umbral", "activa"],
        "permitidos": {"condicion": ["precio_mayor_que", "precio_menor_que", "p_vl_menor_que", "p_vl_mayor_que",
                                     "z_p_vl_menor_que", "z_p_vl_mayor_que", "rendimiento_dividendo_mayor_que"],
                       "activa": ["si", "no"]},
    },
    "dim_costo_intermediario": {
        "claves": ["intermediario", "producto"],
        "columnas": {
            "intermediario": pl.Utf8, "producto": pl.Utf8, "comision_pct": pl.Float64, "comision_minima": pl.Float64,
            "custodia_anual_pct": pl.Float64, "vigente_desde": pl.Date, "fuente": pl.Utf8,
        },
        "obligatorias": ["intermediario", "producto"],
        "permitidos": {},
    },
}

# Signo del movimiento de acciones y de efectivo de cada tipo de transacción (ver cartera.py)
TIPOS_CON_ESPECIE = {"compra", "venta", "dividendo", "dividendo_acciones"}

EJEMPLOS = {
    "dim_perfil": {"perfil_id": "largo_plazo", "version": 1, "nombre": "Largo plazo", "objetivo": "crecimiento",
                   "horizonte_anios": 15, "moneda_base": "COP", "perdida_maxima_aceptable": 0.25,
                   "volatilidad_objetivo": 0.15, "meta_retorno_real": 0.05, "liquidez_reserva": 0.05,
                   "benchmark": "universo_igual", "maximo_por_emisor": 0.15, "excluidos": "",
                   "notas": "Ejemplo: borrar o editar", "vigente_desde": date(2026, 1, 1), "vigente_hasta": None},
    "dim_perfil_asignacion": {"perfil_id": "largo_plazo", "version": 1, "clase_activo_id": "ACCION", "pais_id": "CO",
                              "moneda_id": "COP", "peso_objetivo": 0.6, "banda_minima": 0.5, "banda_maxima": 0.7},
    "dim_cartera": {"cartera_id": "principal", "nombre": "Cartera principal", "perfil_id": "largo_plazo",
                    "tipo": "real", "moneda_base": "COP", "intermediario": "", "creada": date(2026, 1, 1),
                    "copiada_de": None, "notas": "Ejemplo"},
    "fact_transaccion": {"transaccion_id": "ejemplo-1", "cartera_id": "principal", "fecha_operacion": date(2026, 1, 15),
                         "fecha_liquidacion": date(2026, 1, 17), "tipo": "compra", "especie": "coc04paao008",
                         "cantidad": 1000, "precio": 1900, "monto": 1900000, "comision": 5000, "gmf": 0, "retencion": 0,
                         "moneda": "COP", "trm": None, "notas": "Ejemplo"},
    "dim_tesis": {"cartera_id": "principal", "especie": "coc04paao008", "tesis": "Ejemplo de tesis",
                  "catalizador": "", "precio_objetivo": 2500, "condicion_invalidacion": "", "fecha_revision": date(2026, 6, 30),
                  "expectativa_retorno": 0.15, "horizonte_meses": 12, "creada": date(2026, 1, 15), "actualizada": None},
    "dim_parametro_tributario": {"regla_id": "pendiente", "descripcion": "Llenar con un contador", "tipo_rendimiento": "",
                                 "tarifa": 0.0, "base_minima": None, "vigente_desde": date(2026, 1, 1), "vigente_hasta": None,
                                 "fuente": "", "validado_por": ""},
    "dim_alerta": {"alerta_id": "ejemplo-1", "especie": "coc04paao008", "condicion": "precio_menor_que", "umbral": 2000,
                   "activa": "si", "nota": "Ejemplo: avisar si Ecopetrol baja de 2.000", "creada": date(2026, 11, 25)},
    "dim_costo_intermediario": {"intermediario": "Ejemplo", "producto": "acciones_colombia", "comision_pct": 0.003,
                                "comision_minima": 0, "custodia_anual_pct": 0, "vigente_desde": date(2026, 1, 1),
                                "fuente": "tarifario del intermediario"},
}


def carpeta() -> Path:
    c = data_dir() / "usuario"
    c.mkdir(parents=True, exist_ok=True)
    return c


def vacia(tabla: str) -> pl.DataFrame:
    return pl.DataFrame(schema=TABLAS[tabla]["columnas"])


def leer(tabla: str) -> pl.DataFrame:
    archivo = carpeta() / f"{tabla}.parquet"
    if not archivo.exists():
        return vacia(tabla)
    df = pl.read_parquet(archivo)
    faltan = [c for c in TABLAS[tabla]["columnas"] if c not in df.columns]
    return df.with_columns([pl.lit(None, TABLAS[tabla]["columnas"][c]).alias(c) for c in faltan]) \
        .select(list(TABLAS[tabla]["columnas"]))


def validar(tabla: str, df: pl.DataFrame) -> list[str]:
    """Lista de problemas (vacía si la tabla se puede guardar)."""
    d = TABLAS[tabla]
    problemas = [f"falta la columna {c}" for c in d["obligatorias"] if c not in df.columns]
    if problemas:
        return problemas
    try:
        df = df.select([pl.col(c).cast(t, strict=True) if c in df.columns else pl.lit(None, t).alias(c)
                        for c, t in d["columnas"].items()])
    except Exception as e:  # noqa: BLE001
        return [f"tipo de dato inválido: {e}"]
    for c in d["obligatorias"]:
        n = df.get_column(c).null_count()
        if n:
            problemas.append(f"{c}: {n} filas vacías")
    for c, valores in d["permitidos"].items():
        malos = df.filter(pl.col(c).is_not_null() & ~pl.col(c).is_in(valores)).get_column(c).unique().to_list()
        if malos:
            problemas.append(f"{c}: valores no permitidos {malos} (permitidos: {valores})")
    dup = df.group_by(d["claves"]).len().filter(pl.col("len") > 1)
    if dup.height:
        problemas.append(f"claves repetidas en {d['claves']}: {dup.height}")
    if tabla == "fact_transaccion":
        sin_especie = df.filter(pl.col("tipo").is_in(list(TIPOS_CON_ESPECIE)) & pl.col("especie").is_null()).height
        if sin_especie:
            problemas.append(f"{sin_especie} transacciones de compra, venta o dividendo sin especie")
        sin_cantidad = df.filter(pl.col("tipo").is_in(["compra", "venta", "dividendo_acciones"])
                                 & (pl.col("cantidad").fill_null(0) <= 0)).height
        if sin_cantidad:
            problemas.append(f"{sin_cantidad} compras, ventas o dividendos en acciones sin cantidad positiva")
        usd_sin_trm = df.filter((pl.col("moneda") == "USD") & pl.col("trm").is_null()).height
        if usd_sin_trm:
            problemas.append(f"{usd_sin_trm} transacciones en USD sin TRM aplicada")
    if tabla == "dim_perfil_asignacion":
        fuera = df.filter((pl.col("banda_minima") > pl.col("peso_objetivo")) | (pl.col("banda_maxima") < pl.col("peso_objetivo"))).height
        if fuera:
            problemas.append(f"{fuera} filas con el peso objetivo fuera de sus bandas")
    return problemas


def guardar(tabla: str, df: pl.DataFrame) -> list[str]:
    """Valida y reescribe la tabla completa. Devuelve los problemas; si hay, no escribe."""
    problemas = validar(tabla, df)
    if problemas:
        return problemas
    d = TABLAS[tabla]
    df = df.select([pl.col(c).cast(t) if c in df.columns else pl.lit(None, t).alias(c) for c, t in d["columnas"].items()])
    df.sort(d["claves"]).write_parquet(carpeta() / f"{tabla}.parquet")
    return []


def nuevo_id() -> str:
    return uuid.uuid4().hex[:12]


def plantillas() -> list[Path]:
    """Crea las tablas vacías que falten y un CSV de ejemplo por tabla."""
    escritos = []
    dir_plantillas = carpeta() / "plantillas"
    dir_plantillas.mkdir(exist_ok=True)
    for tabla in TABLAS:
        archivo = carpeta() / f"{tabla}.parquet"
        if not archivo.exists():
            vacia(tabla).write_parquet(archivo)
            escritos.append(archivo)
        ejemplo = pl.DataFrame([EJEMPLOS[tabla]], schema=TABLAS[tabla]["columnas"])
        destino = dir_plantillas / f"{tabla}.csv"
        ejemplo.write_csv(destino)
        escritos.append(destino)
    (dir_plantillas / "LEEME.txt").write_text(
        "Plantillas de ejemplo (una fila cada una) para cargar datos desde Excel.\n"
        "Generadas el " + datetime.now(timezone.utc).strftime("%Y-%m-%d") + " por invest usuario --plantillas.\n"
        "Las tablas reales están en la carpeta de arriba (.parquet); la app las escribe validadas.\n",
        encoding="utf-8")
    return escritos


def importar_csv(tabla: str, ruta: Path, reemplazar: bool = False) -> list[str]:
    """Carga un CSV con las columnas de la tabla (las demás se ignoran) y lo une a lo existente."""
    nuevo = pl.read_csv(ruta, try_parse_dates=True, infer_schema_length=None)
    columnas = TABLAS[tabla]["columnas"]
    nuevo = nuevo.select([c for c in nuevo.columns if c in columnas])
    if tabla == "fact_transaccion" and "transaccion_id" not in nuevo.columns:
        nuevo = nuevo.with_columns(pl.Series("transaccion_id", [nuevo_id() for _ in range(nuevo.height)]))
    base = vacia(tabla) if reemplazar else leer(tabla)
    try:
        nuevo = nuevo.select([pl.col(c).cast(t) if c in nuevo.columns else pl.lit(None, t).alias(c) for c, t in columnas.items()])
    except Exception as e:  # noqa: BLE001
        return [f"tipo de dato inválido: {e}"]
    return guardar(tabla, pl.concat([base, nuevo]))
