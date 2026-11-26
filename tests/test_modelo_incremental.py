"""Recálculo incremental de ``invest modelo``: salta los pasos cuyos insumos no cambiaron."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from invest import cli, modelo, storage, usuario


@pytest.fixture
def datos(carpeta_datos):
    import app_datos_util

    app_datos_util.construir()  # primera corrida: calcula todo y deja las huellas
    return carpeta_datos


def _recalculados(resumen: dict) -> set[str]:
    return {n for n, r in resumen.items() if not r.get("saltado")}


def test_segunda_corrida_sin_cambios_salta_todo(datos, capsys):
    assert modelo.ruta_huellas().exists()
    assert (modelo.ruta_huellas().parent / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")
    resumen = modelo.calcular(todo=False)
    assert list(resumen) == list(modelo.PASOS) and _recalculados(resumen) == set()
    # Reescribir un archivo con el mismo contenido (otra fecha de modificación) no cuenta como cambio
    archivo = next((datos / "gold" / "fact_precio_diario").glob("**/*.parquet"))
    pl.read_parquet(archivo).write_parquet(archivo)
    assert _recalculados(modelo.calcular(todo=False)) == set()
    # La línea de comandos muestra los saltados y --todo los fuerza
    assert cli.main(["modelo"]) in (0, 1)  # 1 si el contrato tiene problemas (datos mínimos)
    salida = capsys.readouterr().out
    n = len(modelo.PASOS)
    assert "tecnico: saltado" in salida and f"{n} saltados" in salida
    cli.main(["modelo", "--todo"])
    salida = capsys.readouterr().out
    assert ": saltado" not in salida and f"{n} recalculados, 0 saltados" in salida


def test_cambiar_precios_recalcula_el_paso_y_sus_dependientes(datos):
    df = storage.leer_tabla("gold", "fact_precio_diario")
    ultima = df.get_column("fecha").max()
    df = df.with_columns(pl.when(pl.col("fecha") == ultima).then(pl.col("precio") * 1.05).otherwise(pl.col("precio"))
                         .alias("precio"))
    storage.escribir_por_anio("gold", "fact_precio_diario", df, "fecha")
    resumen = modelo.calcular(todo=False)
    # Leen precios: técnico, métricas, bandas y cartera; riesgo y puntajes leen lo que escribe técnico
    assert _recalculados(resumen) == {"tecnico", "riesgo", "metricas", "bandas", "puntajes", "cartera"}
    assert resumen["calendario"] == {"saltado": True} and resumen["variables"] == {"saltado": True}
    assert _recalculados(modelo.calcular(todo=False)) == set()  # y vuelve a quedar al día


def test_cambiar_macro_solo_toca_lo_que_la_lee(datos):
    df = storage.leer_tabla("gold", "fact_macro")
    storage.escribir_por_anio("gold", "fact_macro", df.with_columns(pl.col("valor") * 1.01), "fecha")
    assert _recalculados(modelo.calcular(todo=False)) == {"riesgo", "calendario", "puntajes"}


def test_cambiar_transacciones_recalcula_solo_la_cartera(datos):
    t = usuario.leer("fact_transaccion")
    nueva = t.head(1).with_columns(pl.lit("extra-1").alias("transaccion_id"), pl.lit("aporte").alias("tipo"),
                                   pl.lit(None, pl.Utf8).alias("especie"), pl.lit(1_000_000.0).alias("monto"))
    assert usuario.guardar("fact_transaccion", pl.concat([t, nueva])) == []
    assert _recalculados(modelo.calcular(todo=False)) == {"cartera"}


def test_salida_borrada_o_dia_nuevo_se_recalculan(datos):
    for archivo in (datos / "gold" / "fact_riesgo").glob("**/*.parquet"):
        archivo.unlink()
    assert _recalculados(modelo.calcular(todo=False)) == {"riesgo", "puntajes"}
    # El calendario separa pasado y futuro con la fecha del día (y la cartera, sin precios, llega hasta hoy)
    manana = date.today() + timedelta(days=1)
    assert _recalculados(modelo.calcular(todo=False, hoy=manana)) == {"calendario", "cartera"}


def test_un_paso_que_falla_se_repite_en_la_proxima_corrida(datos, monkeypatch):
    from invest import puntajes

    def falla():
        raise RuntimeError("se cayó")

    with monkeypatch.context() as m:
        m.setattr(puntajes, "construir", falla)
        with pytest.raises(RuntimeError):
            modelo.calcular(todo=True)
    assert _recalculados(modelo.calcular(todo=False)) == {"puntajes"}


def test_huellas_danadas_recalculan_todo(datos):
    modelo.ruta_huellas().write_text("{no es json", encoding="utf-8")
    assert _recalculados(modelo.calcular(todo=False)) == set(modelo.PASOS)


def test_pasos_declaran_lo_que_su_codigo_lee_y_escribe():
    """PASOS debe coincidir con las lecturas y escrituras del código de cada módulo."""
    carpeta = Path(modelo.__file__).parent
    for nombre, paso in modelo.PASOS.items():
        texto = (carpeta / f"{paso['modulo']}.py").read_text(encoding="utf-8")
        leidas = {(c, t) for c, t in re.findall(r'storage\.leer_tabla\("(\w+)", "([\w/=]+)"\)', texto)}
        leidas |= {("gold", t) for t in re.findall(r'storage\.leer_dim\("(\w+)"\)', texto)}
        leidas |= {("usuario", t) for t in re.findall(r'usuario\.leer\("(\w+)"\)', texto)}
        escritas = {(c, t) for c, t in re.findall(r'storage\.escribir_\w+\("(\w+)", "(\w+)"', texto)}
        config = set(re.findall(r'cargar_yaml\("([\w./]+)"\)', texto))
        if 'CONFIG_DIR / "variables"' in texto:
            config.add("variables/*.yaml")
        assert leidas == set(paso["entradas"]), nombre
        assert escritas == set(paso["salidas"]), nombre
        assert config == set(paso.get("config", [])), nombre
        assert ("date.today()" in texto) == bool(paso.get("hoy")), nombre
    # Orden válido: ningún paso lee lo que escribe uno posterior
    orden = list(modelo.PASOS)
    for i, nombre in enumerate(orden):
        posteriores = {s for otro in orden[i + 1:] for s in modelo.PASOS[otro]["salidas"]}
        assert not set(modelo.PASOS[nombre]["entradas"]) & posteriores, nombre


def test_avisa_si_una_tabla_de_dims_es_anterior_a_su_codigo(datos):
    import os

    archivos = modelo._archivos_tabla("gold", "fact_precio_diario")
    assert archivos and "fact_precio_diario" not in modelo.tablas_de_dims_atrasadas()
    for a in archivos:
        os.utime(a, (1, 1))  # 1970: más vieja que mercado.py
    assert "fact_precio_diario" in modelo.tablas_de_dims_atrasadas()


def test_modelo_con_dims_reconstruye_los_precios(datos, monkeypatch, capsys):
    llamado = []
    monkeypatch.setattr(cli.dims, "construir", lambda: llamado.append(1) or {"fact_precio_diario": "ok"})
    cli.main(["modelo", "--con-dims"])
    assert llamado == [1] and "dims:" in capsys.readouterr().out
    from invest.app import servidor
    assert servidor.ACCIONES["actualizar_modelo"] == ["modelo", "--con-dims"]
