"""Línea de comandos.

    invest dims                         construye las dimensiones (gold/dims)
    invest ingest trm_datos_gov         ingesta incremental de la TRM
    invest ingest trm_datos_gov --desde 2024-01-01 --hasta 2024-12-31
    invest ingest --todas-actions       corre todos los conectores implementados que van en GitHub Actions
    invest ingest --todas-pc            corre los que van en el PC (Programador de tareas)
    invest salud                        reporte de salud de datos
    invest alertas [--nuevas] [--json]  alertas de cartera, tesis, dividendos, valoración y reglas propias
    invest inspeccionar [especie]       prueba en vivo las tres piezas de la SFC sin guardar nada
    invest dividendos                   extrae los dividendos de los PDF con dos agentes y consenso
    invest dividendos --revision        solo regenera el archivo de revisión y la tabla de dividendos
    invest dividendos --reevaluar       vuelve a juzgar las disputas con las lecturas guardadas (sin API)
    invest factores                     calcula los factores mensuales y su validación estadística
    invest ficha [--abrir]              puntaje de los factores validados por acción y tablero local (HTML)
    invest modelo [--sin-calcular]      tablas del aplicativo, base modelo.duckdb y verificación del contrato
    invest modelo --todo                recalcula todos los pasos aunque sus insumos no hayan cambiado
    invest usuario --plantillas         tablas vacías y CSV de ejemplo en invest-data/usuario/
    invest app [--puerto 8790] [--no-abrir]   aplicativo local en el navegador (requiere el extra app)
    invest usuario --importar TABLA ARCHIVO.csv   carga perfiles, carteras o transacciones validadas
    invest probar-niif 260 36 2025-09-30 [--visible]   prueba la página del SIMEV con un emisor (no guarda)
"""

from __future__ import annotations

import argparse

import polars as pl
import sys
from datetime import date

from invest import dims, salud
from invest.config import catalogo_fuentes, data_dir
from invest.ingest import CONECTORES


def _fecha(s: str) -> date:
    return date.fromisoformat(s)


def _ingestar(ids: list[str], desde: date | None, hasta: date | None) -> int:
    codigo = 0
    for fid in ids:
        if fid not in CONECTORES:
            print(f"[omitido] {fid}: conector aún no implementado")
            continue
        res = CONECTORES[fid]().ejecutar(desde, hasta)
        print(res.resumen())
        if res.estado == "error":
            codigo = 1
    return codigo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="invest", description="Sistema de análisis de activos")
    sub = p.add_subparsers(dest="comando", required=True)

    sub.add_parser("dims", help="Construye las dimensiones")

    pi = sub.add_parser("ingest", help="Ingesta de una o varias fuentes")
    pi.add_argument("fuentes", nargs="*", help="ids de config/catalogo_fuentes.yaml")
    pi.add_argument("--desde", type=_fecha)
    pi.add_argument("--hasta", type=_fecha)
    grupo = pi.add_mutually_exclusive_group()
    grupo.add_argument("--todas-actions", action="store_true")
    grupo.add_argument("--todas-pc", action="store_true")

    pa = sub.add_parser("alertas", help="Alertas de cartera, tesis, dividendos, valoración y reglas propias")
    pa.add_argument("--nuevas", action="store_true", help="solo las que no se habían mostrado (y las marca como vistas)")
    pa.add_argument("--json", action="store_true", help="salida en JSON (para la notificación de Windows)")

    ps = sub.add_parser("salud", help="Reporte de salud de datos")
    ps.add_argument("--solo", choices=["actions", "pc"],
                    help="el código de salida solo cuenta las fuentes que corren ahí (las demás se muestran igual)")

    pdiv = sub.add_parser("dividendos", help="Extrae dividendos de los PDF con dos agentes")
    pdiv.add_argument("--max", type=int, help="máximo de documentos en esta corrida")
    pdiv.add_argument("--reprocesar", action="store_true", help="vuelve a leer también los ya procesados")
    pdiv.add_argument("--revision", action="store_true", help="solo regenera revision/dividendos.csv y gold")
    pdiv.add_argument("--reevaluar", action="store_true",
                      help="vuelve a juzgar los documentos en disputa con las lecturas guardadas, sin llamar a la API")

    sub.add_parser("factores", help="Factores mensuales y su validación estadística")

    pm = sub.add_parser("modelo", help="Tablas del aplicativo, base DuckDB y verificación del contrato")
    modo = pm.add_mutually_exclusive_group()
    modo.add_argument("--sin-calcular", action="store_true", help="solo rehace la base y verifica")
    modo.add_argument("--todo", action="store_true",
                      help="recalcula todos los pasos (por omisión salta los que no tienen insumos nuevos)")
    pm.add_argument("--con-dims", action="store_true",
                    help="corre antes invest dims (precios, retornos, liquidez, fundamentales): lo usa el botón del aplicativo")

    pa = sub.add_parser("app", help="Aplicativo local en el navegador")
    pa.add_argument("--puerto", type=int, default=8790, help="si está ocupado por otro programa, usa el siguiente libre")
    pa.add_argument("--no-abrir", action="store_true", help="no abre el navegador")

    pu = sub.add_parser("usuario", help="Datos que ingresa el usuario (perfiles, carteras, transacciones)")
    pu.add_argument("--plantillas", action="store_true", help="crea tablas vacías y CSV de ejemplo")
    pu.add_argument("--importar", nargs=2, metavar=("TABLA", "ARCHIVO"), help="carga un CSV validado")
    pu.add_argument("--reemplazar", action="store_true", help="con --importar: reemplaza en vez de agregar")

    pf = sub.add_parser("ficha", help="Puntaje de los factores validados por acción y tablero local")
    pf.add_argument("--abrir", action="store_true", help="abre el tablero en el navegador")

    pn = sub.add_parser("probar-niif", help="Prueba la página de informes NIIF del SIMEV con un emisor (no guarda)")
    pn.add_argument("tipo", type=int)
    pn.add_argument("codigo", type=int)
    pn.add_argument("corte", type=_fecha)
    pn.add_argument("--visible", action="store_true", help="abre una ventana de Chromium")

    pv = sub.add_parser("estados-ventana", help="Lectura manual con ventana de los estados de las compañías "
                        "con sitio protegido (la lanza una persona; luego los procesa)")
    pv.add_argument("--emisor", nargs="*", help="emisor_id (por omisión, todos los marcados ventana: true)")
    pv.add_argument("--abierta", type=int, default=120, help="segundos que la ventana queda abierta por emisor")
    pv.add_argument("--sin-procesar", action="store_true", help="solo guarda los PDF; no corre estados_compania")

    sub.add_parser("cobertura", help="Qué documentos faltan por emisor y por dónde conseguirlos (revision/cobertura_documentos.csv)")

    pins = sub.add_parser("inspeccionar", help="Prueba en vivo la estructura de la SFC (no guarda)")
    pins.add_argument("especie", nargs="?", default="coc04paao008")

    a = p.parse_args(argv)

    if a.comando == "inspeccionar":
        from invest.ingest.sfc import inspeccionar

        r = inspeccionar(a.especie)
        print(f"Índice: {r['indice']['especies']} especies, fecha de reporte {r['indice']['fecha_reporte']}")
        print("Ficha:")
        for k, v in r["ficha"].items():
            print(f"  {k}: {v}")
        s = r["serie"]
        print(f"Serie {s['archivo']}: {s['filas']} filas, columnas {s['columnas']}")
        print(f"  primera: {s['primera']}")
        print(f"  última:  {s['ultima']}")
        faltan = [k for k in ("codigo_accion", "fecha_reporte", "precio_promedio", "iba") if r["ficha"].get(k) is None]
        ok = r["indice"]["especies"] > 0 and s["filas"] > 0 and not faltan
        print("Estructura reconocida" if ok else f"REVISAR: faltan {faltan or 'filas en la serie'}")
        return 0 if ok else 1

    if not getattr(a, "json", False):  # con --json la salida debe ser solo el JSON
        print(f"Carpeta de datos: {data_dir()}")

    if a.comando == "dims":
        for nombre, ruta in dims.construir().items():
            print(f"  {nombre}: {ruta}")
        return 0

    if a.comando == "cobertura":
        from invest import cobertura

        t, ruta = cobertura.escribir()
        con_vacios = t.filter(pl.col("diagnostico") != "")
        print(f"{t.height} emisores; {con_vacios.height} con algún vacío. Detalle: {ruta}")
        for r in con_vacios.iter_rows(named=True):
            print(f"  {r['nombre'][:28]:28} {r['diagnostico']}")
        return 0

    if a.comando == "estados-ventana":
        from invest.ingest import estados_ventana

        try:
            resumen = estados_ventana.leer(a.emisor or None, abierta_s=a.abierta)
        except estados_ventana.SinPersona as e:
            print(f"No se lee: {e}")
            return 2
        nuevos = sum(r["guardados"] + r["abiertos"] for r in resumen.values())
        print(f"PDF nuevos guardados: {nuevos}")
        if nuevos and not a.sin_procesar:
            return _ingestar(["estados_compania"], None, None)
        return 0

    if a.comando == "ingest":
        ids = list(a.fuentes)
        if a.todas_actions or a.todas_pc:
            destino = "actions" if a.todas_actions else "pc"
            ids = [
                f["id"] for f in catalogo_fuentes()
                if f.get("estado") == "implementado" and f.get("ejecuta_en") == destino
            ]
        if not ids:
            print("Indique al menos una fuente o use --todas-actions / --todas-pc")
            return 2
        return _ingestar(ids, a.desde, a.hasta)

    if a.comando == "dividendos":
        from invest import dividendos

        if a.reevaluar:
            conteo = dividendos.reevaluar()
            print("Disputas: " + ", ".join(f"{k} {v}" for k, v in conteo.items()))
        elif a.revision:
            ruta = dividendos.exportar()
            print(f"Archivo de revisión: {ruta or 'aún no hay extracciones'}")
        else:
            conteo = dividendos.procesar(maximo=a.max, reprocesar=a.reprocesar)
            # Solo conteos: los registros de GitHub Actions de un repo público los ve cualquiera
            print("Dividendos: " + ", ".join(f"{k} {v}" for k, v in conteo.items()))
        malas = dividendos.revisiones_invalidas()
        if malas:
            print(f"Valores no reconocidos en la columna revision (se tratan como sin revisar): {malas}")
        return 0

    if a.comando == "probar-niif":
        from invest.ingest.sfc_niif import PaginaSIMEV

        with PaginaSIMEV(visible=a.visible, bitacora=True) as pagina:
            r = pagina.informe(a.tipo, a.codigo, a.corte)
            carpeta = data_dir() / "diagnostico"
            carpeta.mkdir(parents=True, exist_ok=True)
            captura = carpeta / f"simev_{a.tipo}_{a.codigo}_{a.corte}.png"
            pagina.pagina.screenshot(path=str(captura), full_page=True)
        if r is None:
            print("La página no cargó los selectores")
        else:
            formatos = sorted({(x["formato"], x["consolidado"]) for x in r["anexos"]})
            print(f"Anexos: {len(r['anexos'])} celdas, formatos {formatos}; XBRL: {'sí' if r['xbrl'] else 'no'}")
        print(f"Captura: {captura}")
        return 0

    if a.comando == "factores":
        from invest import factores

        tabla, resumen = factores.construir()
        print("Factores: " + ", ".join(f"{k} {v}" for k, v in resumen.items()))
        val, veredictos = factores.validar(tabla)
        if val is not None:
            print("Validación: " + ", ".join(f"{k} {v}" for k, v in veredictos.items()))
            with __import__("polars").Config(tbl_rows=20, tbl_cols=12, float_precision=3, tbl_width_chars=200):
                print(val.select("factor", "meses", "ic_medio", "ic_t", "ic_p_holm", "ic_medio_descubrimiento",
                                 "ic_medio_fuera_muestra", "exceso_superior", "exceso_superior_neto",
                                 "rotacion_superior", "veredicto"))
        return 0

    if a.comando == "ficha":
        from invest import ficha, tablero

        tablas, resumen = ficha.construir()
        if not tablas:
            print("Sin factores validados o sin gold/fact_factor_mensual: corra invest factores primero.")
            return 1
        print("Ficha: " + ", ".join(f"{k} {v}" for k, v in resumen.items()))
        destino = tablero.escribir()
        print(f"Tablero: {destino}")
        if a.abrir and destino:
            import webbrowser

            webbrowser.open(destino.resolve().as_uri())
        return 0

    if a.comando == "modelo":
        from invest import modelo

        if a.con_dims:
            print("dims: construyendo precios, retornos, liquidez, fundamentales y macro", flush=True)
            for nombre, ruta in dims.construir().items():
                print(f"  {nombre}: {ruta}", flush=True)

        def informar(nombre: str, res: dict) -> None:
            if res.get("saltado"):
                print(f"{nombre}: saltado (sin cambios en sus insumos)", flush=True)
            else:
                print(f"{nombre}: " + (", ".join(f"{k} {v}" for k, v in res.items()) or "sin datos"), flush=True)

        atrasadas = modelo.tablas_de_dims_atrasadas()
        if atrasadas:
            print("Aviso: el código cambió después de construir " + ", ".join(atrasadas)
                  + ". Corra primero uv run invest dims (invest modelo no las reconstruye).", flush=True)
        r = modelo.construir(recalcular=not a.sin_calcular, todo=a.todo, informar=informar)
        if r["calculo"]:
            hechos = len(r["calculo"]) - len(r["saltados"])
            print(f"Pasos: {hechos} recalculados, {len(r['saltados'])} saltados"
                  + (f" ({', '.join(r['saltados'])})" if r["saltados"] else "")
                  + ("" if a.todo or not r["saltados"] else "; invest modelo --todo los fuerza"))
        print(f"Base: {r['duckdb']} ({r['vistas']} vistas)")
        for p in r["problemas"]:
            print(f"[contrato] {p}")
        print("Contrato: " + ("sin problemas" if not r["problemas"] else f"{len(r['problemas'])} problemas"))
        return 1 if r["problemas"] else 0

    if a.comando == "app":
        try:
            from invest.app.servidor import iniciar
        except ImportError:
            print("Falta el extra app: uv sync --extra app (o uv run --extra app invest app)")
            return 1
        iniciar(a.puerto, abrir=not a.no_abrir)
        return 0

    if a.comando == "usuario":
        from pathlib import Path

        from invest import usuario

        if a.plantillas:
            for ruta in usuario.plantillas():
                print(f"Creado: {ruta}")
        if a.importar:
            tabla, archivo = a.importar
            if tabla not in usuario.TABLAS:
                print(f"Tabla desconocida: {tabla}. Opciones: {', '.join(usuario.TABLAS)}")
                return 1
            problemas = usuario.importar_csv(tabla, Path(archivo), reemplazar=a.reemplazar)
            for p in problemas:
                print(f"[no se guardó] {p}")
            if problemas:
                return 1
            print(f"{tabla}: {usuario.leer(tabla).height} filas")
        return 0

    if a.comando == "alertas":
        import json as _json

        import duckdb

        from invest import alertas

        base = data_dir() / "modelo.duckdb"
        if not base.exists():
            print("No existe modelo.duckdb: corra invest modelo")
            return 1
        with duckdb.connect(str(base), read_only=True) as con:
            lista = alertas.calcular(con)
        if a.nuevas:
            lista = alertas.nuevas(lista)
        if a.json:
            print(_json.dumps(lista, ensure_ascii=False, default=str))
        else:
            for x in lista:
                print(f"[{'!' if x['severidad'] == 'atencion' else 'i'}] {x['titulo']} — {x['detalle']}")
            if not lista:
                print("Sin alertas" + (" nuevas" if a.nuevas else ""))
        return 0

    if a.comando == "salud":
        df = salud.evaluar()
        with __import__("polars").Config(tbl_rows=50, tbl_cols=10, fmt_str_lengths=40):
            print(df.select(["fuente_id", "ultimo_dato", "rezago_dias_habiles", "estado"]))
        malos = df.filter(~df["estado"].str.starts_with("al día") & ~df["estado"].str.starts_with("manual"))
        avisos = df.filter(df["estado"] == "al día (falló la última corrida)")
        if not avisos.is_empty():
            print("Aviso: falló la última corrida, pero el dato sigue al día (se reintenta en la próxima): "
                  + ", ".join(avisos["fuente_id"].to_list()))
        if a.solo:
            # En GitHub Actions, una fuente del PC atrasada (p. ej. la carga NIIF) no es una falla de la ingesta
            # de la nube: se avisa, pero no se marca la corrida en rojo
            propias = {f["id"] for f in catalogo_fuentes() if f.get("ejecuta_en") == a.solo}
            ajenas = malos.filter(~malos["fuente_id"].is_in(list(propias)))
            if not ajenas.is_empty():
                print(f"Aviso: atrasadas fuera de {a.solo} (no cuentan para el resultado): "
                      + ", ".join(ajenas["fuente_id"].to_list()))
            malos = malos.filter(malos["fuente_id"].is_in(list(propias)))
        return 0 if malos.is_empty() else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
