"""Cobertura de documentos por emisor: qué falta y por dónde se puede conseguir (``invest cobertura``).

Para cada emisor de ``config/emisores_rnve.yaml`` cuenta los cortes trimestrales desde 2015 con anexos
NIIF del SIMEV (con informe, sin informe publicado o aún sin revisar por la carga histórica), los XBRL
consolidados, los estados de la compañía leídos de PDF, los años de Supersociedades, los dividendos
(PDF de la SFC e historiales web) y la historia de precios. Escribe ``revision/cobertura_documentos.csv``
con un diagnóstico y la fuente alterna propuesta para cada vacío. Solo conteos y fechas: ninguna cifra.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import yaml

from invest import storage, sucesiones
from invest.config import CONFIG_DIR, cargar_yaml

ARCHIVO = ("revision", "cobertura_documentos.csv")


def _cortes(hasta: date) -> list[date]:
    return [date(y, m, d) for y in range(2015, hasta.year + 1) for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))
            if date(y, m, d) <= hasta]


def _ultimo_corte_vencido(hoy: date) -> date:
    """El corte más reciente cuyo plazo de publicación (unos 45 días; 100 para diciembre) ya pasó."""
    for c in reversed(_cortes(hoy)):
        if (hoy - c).days >= (100 if c.month == 12 else 50):
            return c
    return date(2015, 3, 31)


def tabla(hoy: date | None = None) -> pl.DataFrame:
    hoy = hoy or date.today()
    emisores = yaml.safe_load((CONFIG_DIR / "emisores_rnve.yaml").read_text(encoding="utf-8"))["emisores"]
    estado = storage.leer_estado_fuente("sfc_rnve_niif").get("informes") or {}
    cortes = _cortes(_ultimo_corte_vencido(hoy))
    bancos = {m["emisor_id"] for m in (cargar_yaml("bancos.yaml").get("mapeo") or []) if m.get("emisor_id")}
    no_bancos_financieros = {"CO-COJ12", "CO-COR01", "CO-COW19", "CO-COR02", "CO-COT13", "CO-COT23", "CO-COT29", "CO-PAT07"}

    def conteo(capa, nombre, por, clave="emisor_id"):
        t = storage.leer_tabla(capa, nombre)
        return {} if t is None or t.is_empty() else dict(t.group_by(clave).agg(por).iter_rows())

    xbrl = storage.leer_tabla("silver", "fact_xbrl_sfc")
    xbrl_cortes = {} if xbrl is None else dict(
        xbrl.select("emisor_id", "fecha_corte").unique().group_by("emisor_id").agg(pl.col("fecha_corte").sort()).iter_rows())
    pdf = conteo("silver", "fact_ee_compania", pl.col("fecha_corte").n_unique())
    ss = conteo("silver", "fact_ee_anual_supersociedades", pl.col("anio").n_unique())
    div_pdf = conteo("gold", "fact_dividendo", pl.len())
    activos = storage.leer_dim("dim_activo")
    act = activos.select("especie", "emisor_id", "nemotecnico") if activos is not None else pl.DataFrame(
        schema={"especie": pl.Utf8, "emisor_id": pl.Utf8, "nemotecnico": pl.Utf8})
    web = storage.leer_tabla("gold", "fact_dividendo_web")
    div_web = {} if web is None else dict(web.join(act, on="especie").group_by("emisor_id").agg(pl.len()).iter_rows())
    precios = sucesiones.precios_sfc()
    desde = {} if precios is None else dict(
        precios.filter(pl.col("valor_transado").fill_null(0) > 0).join(act, on="especie")
        .group_by("emisor_id").agg(pl.col("fecha").min()).iter_rows())
    emisor_de = dict(act.select("especie", "emisor_id").iter_rows())
    sin_historia = set()
    for suc in sucesiones.cargar():
        heredadas = 0 if precios is None else precios.filter(
            (pl.col("especie") == suc["especie"]) & pl.col("fuente_id").str.starts_with("sucesion")).height
        if not heredadas and emisor_de.get(suc["especie"]):
            sin_historia.add(emisor_de[suc["especie"]])
    est_cfg = {}
    try:
        from invest.ingest import estados_compania
        est_cfg = estados_compania.emisores_config()
    except Exception:  # noqa: BLE001 - sin configuración de estados de compañías
        pass

    filas = []
    for e in emisores:
        i = e["emisor_id"]
        con = [c for c in cortes if (estado.get(f"{i}|{c}") or {}).get("anexos", 0) > 0]
        sin = [c for c in cortes if f"{i}|{c}" in estado and not estado[f"{i}|{c}"].get("anexos")]
        pend = [c for c in cortes if f"{i}|{c}" not in estado]
        xb = [c for c in xbrl_cortes.get(i, []) if c in cortes]
        diag, alterna = [], []
        if pend:
            diag.append(f"{len(pend)} cortes sin revisar todavía")
            alterna.append("terminar la carga histórica de NIIF en el PC")
        inicio = desde.get(i)
        antes = [c for c in sin if inicio and c < inicio and i not in sin_historia]
        sin = [c for c in sin if c not in antes]
        if antes:
            diag.append(f"{len(antes)} cortes de antes de cotizar (normal)")
        if sin:
            diciembres = [c for c in sin if c.month == 12]
            if not con:
                diag.append("ningún informe en el SIMEV")
                alterna.append("revisar los códigos del emisor en config/emisores_rnve.yaml")
            elif len(diciembres) >= 3 and i in bancos:
                diag.append(f"el SIMEV no publica {len(diciembres)} diciembres" + (f" ni {len(sin) - len(diciembres)} corte(s) más" if len(sin) > len(diciembres) else ""))
                alterna.append("valor y utilidad por acción del CUIF (automático, cotejado con los anexos)")
            elif len(diciembres) >= 3:
                diag.append(f"el SIMEV no publica {len(diciembres)} diciembres")
                alterna.append("revisar en ventana el periodo de diciembre del SIMEV; respaldo: estados anuales en el sitio de la compañía")
            elif min(sin) > max(con):
                diag.append(f"sin informes desde {min(sin):%Y-%m}")
                alterna.append("confirmar si sigue inscrito (información relevante de la SFC)")
            else:
                diag.append(f"{len(sin)} cortes sin informe ({', '.join(f'{c:%Y-%m}' for c in sin[:4])})")
                alterna.append("información relevante de la SFC o sitio de la compañía")
        financiero = i in bancos or i in no_bancos_financieros
        if not xb and not pdf.get(i) and not financiero:
            cfg_e = est_cfg.get(i)
            if cfg_e is None:
                diag.append("sin estados consolidados")
                alterna.append("agregar su sitio a config/estados_compania.yaml")
            elif cfg_e.get("descarga") == "pendiente":
                diag.append("sin estados consolidados (sin dirección verificada)")
                alterna.append(f"PDF a mano en invest-data/fuentes_compania/{i}/")
            elif cfg_e.get("ventana") or cfg_e.get("descarga") == "manual":
                diag.append("sin estados consolidados (sitio protegido)")
                alterna.append("botón «Leer estados de compañías (con ventana)» en Salud, o PDF en fuentes_compania/")
            else:
                diag.append("sin estados consolidados leídos aún")
                alterna.append("próxima corrida de estados_compania en el PC")
        if e.get("suspendida_desde"):
            diag.append(f"negociación suspendida desde {e['suspendida_desde']}")
            alterna.append("sin acción: la acción no negocia; sus informes siguen en el SIMEV si los publica")
        if not div_pdf.get(i) and not div_web.get(i):
            diag.append("sin dividendos registrados")
            alterna.append("historial web (Investing, stockanalysis) o información relevante")
        if i in sin_historia:
            diag.append("precios solo desde la sucesión")
            alterna.append("historia de la antecesora (config/sucesiones.yaml)")
        filas.append({
            "emisor_id": i, "nombre": e["nombre"], "niif_con_informe": len(con), "niif_sin_informe": len(sin),
            "niif_sin_revisar": len(pend), "ultimo_anexo": max(con) if con else None,
            "cortes_sin_informe": ", ".join(f"{c:%Y-%m}" for c in sin), "cortes_antes_de_cotizar": len(antes),
            "xbrl_cortes": len(xb), "ultimo_xbrl": max(xb) if xb else None, "pdf_compania_cortes": pdf.get(i, 0),
            "supersociedades_anios": ss.get(i, 0), "dividendos_pdf": div_pdf.get(i, 0), "dividendos_web": div_web.get(i, 0),
            "precios_desde": desde.get(i), "diagnostico": "; ".join(diag), "fuente_alterna": "; ".join(dict.fromkeys(alterna)),
        })
    return pl.DataFrame(filas)


def escribir(hoy: date | None = None) -> tuple[pl.DataFrame, object]:
    t = tabla(hoy)
    ruta = storage.data_dir().joinpath(*ARCHIVO)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    t.write_csv(ruta)
    return t, ruta
