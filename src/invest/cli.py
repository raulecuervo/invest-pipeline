"""Línea de comandos.

    invest dims                         construye las dimensiones (gold/dims)
    invest ingest trm_datos_gov         ingesta incremental de la TRM
    invest ingest trm_datos_gov --desde 2024-01-01 --hasta 2024-12-31
    invest ingest --todas-actions       corre todos los conectores implementados que van en GitHub Actions
    invest ingest --todas-pc            corre los que van en el PC (Programador de tareas)
    invest salud                        reporte de salud de datos
    invest inspeccionar [especie]       prueba en vivo las tres piezas de la SFC sin guardar nada
    invest dividendos                   extrae los dividendos de los PDF con dos agentes y consenso
    invest dividendos --revision        solo regenera el archivo de revisión y la tabla de dividendos
    invest dividendos --reevaluar       vuelve a juzgar las disputas con las lecturas guardadas (sin API)
    invest factores                     calcula los factores mensuales y su validación estadística
    invest ficha [--abrir]              puntaje de los factores validados por acción y tablero local (HTML)
    invest modelo [--sin-calcular]      tablas del aplicativo, base modelo.duckdb y verificación del contrato
    invest usuario --plantillas         tablas vacías y CSV de ejemplo en invest-data/usuario/
    invest usuario --importar TABLA ARCHIVO.csv   carga perfiles, carteras o transacciones validadas
    invest probar-niif 260 36 2025-09-30 [--visible]   prueba la página del SIMEV con un emisor (no guarda)
"""

from __future__ import annotations

import argparse
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

    sub.add_parser("salud", help="Reporte de salud de datos")

    pdiv = sub.add_parser("dividendos", help="Extrae dividendos de los PDF con dos agentes")
    pdiv.add_argument("--max", type=int, help="máximo de documentos en esta corrida")
    pdiv.add_argument("--reprocesar", action="store_true", help="vuelve a leer también los ya procesados")
    pdiv.add_argument("--revision", action="store_true", help="solo regenera revision/dividendos.csv y gold")
    pdiv.add_argument("--reevaluar", action="store_true",
                      help="vuelve a juzgar los documentos en disputa con las lecturas guardadas, sin llamar a la API")

    sub.add_parser("factores", help="Factores mensuales y su validación estadística")

    pm = sub.add_parser("modelo", help="Tablas del aplicativo, base DuckDB y verificación del contrato")
    pm.add_argument("--sin-calcular", action="store_true", help="solo rehace la base y verifica")

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

    print(f"Carpeta de datos: {data_dir()}")

    if a.comando == "dims":
        for nombre, ruta in dims.construir().items():
            print(f"  {nombre}: {ruta}")
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

        r = modelo.construir(recalcular=not a.sin_calcular)
        for nombre, res in r["calculo"].items():
            print(f"{nombre}: " + (", ".join(f"{k} {v}" for k, v in res.items()) or "sin datos"))
        print(f"Base: {r['duckdb']} ({r['vistas']} vistas)")
        for p in r["problemas"]:
            print(f"[contrato] {p}")
        print("Contrato: " + ("sin problemas" if not r["problemas"] else f"{len(r['problemas'])} problemas"))
        return 1 if r["problemas"] else 0

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

    if a.comando == "salud":
        df = salud.evaluar()
        with __import__("polars").Config(tbl_rows=50, tbl_cols=10, fmt_str_lengths=40):
            print(df.select(["fuente_id", "ultimo_dato", "rezago_dias_habiles", "estado"]))
        malos = df.filter((df["estado"] != "al día") & ~df["estado"].str.starts_with("manual"))
        return 0 if malos.is_empty() else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
