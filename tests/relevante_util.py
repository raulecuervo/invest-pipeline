"""Páginas del formulario de información relevante de la SFC, armadas con el marcado capturado del
sitio el 22-nov-2026 (formulario, página de resultados y respuesta de paginación de PrimeFaces)."""

from __future__ import annotations

import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "sfc_relevante"
BASE = "/ReportesInformacionRelevante/faces/A_rnviRelevantes/consultas/consultaEntidades/"


def filas_reales() -> list[str]:
    texto = (FIXTURES / "filas.html").read_text(encoding="utf-8")
    return re.findall(r'<tr data-ri="\d+".*?</tr>', texto, re.S)


def _vs(valor: str) -> str:
    return (f'<input type="hidden" name="formaIngreso_SUBMIT" value="1" /><input type="hidden" '
            f'name="javax.faces.ViewState" id="j_id__v_0:javax.faces.ViewState:1" value="{valor}" '
            f'autocomplete="off" /></form></body>\n</html>')


def pagina_formulario(vs: str = "VS-FORM") -> str:
    return (f'<html><body><form id="formaIngreso" name="formaIngreso" method="post" '
            f'action="{BASE}consulta.xhtml;jsessionid=SESION" enctype="application/x-www-form-urlencoded">'
            f'<select id="formaIngreso:tema_input" name="formaIngreso:tema_input" tabindex="-1" aria-hidden="true">'
            f'<option value="">Seleccione el estado</option><option value="Todos">Todos</option></select>' + _vs(vs))


def pagina_resultados(filas: list[str], total: int, vs: str = "VS-RES") -> str:
    visibles = "".join(filas[:20])
    return (f'<html><body><form id="formaIngreso" name="formaIngreso" method="post" action="{BASE}busqueda.xhtml" '
            f'enctype="application/x-www-form-urlencoded"><div id="formaIngreso:j_id_d" class="ui-datatable ui-widget">'
            f'<table role="grid"><tbody id="formaIngreso:j_id_d_data" class="ui-datatable-data ui-widget-content">'
            f'{visibles}</tbody></table></div><script>PrimeFaces.cw("DataTable","widget_formaIngreso_j_id_d",'
            f'{{id:"formaIngreso:j_id_d",paginator:{{id:[\'formaIngreso:j_id_d_paginator_top\'],rows:20,'
            f'rowCount:{total},page:0}}}});</script>' + _vs(vs))


def respuesta_parcial(filas: list[str], vs: str = "VS-AJAX") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><partial-response id="j_id__v_0"><changes>'
            '<update id="formaIngreso:messages"><![CDATA[<div id="formaIngreso:messages" class="ui-messages '
            'ui-widget" aria-live="polite"></div>]]></update><update id="formaIngreso:j_id_d"><![CDATA['
            + "".join(filas) + f']]></update><update id="j_id__v_0:javax.faces.ViewState:1"><![CDATA[{vs}]]>'
            '</update></changes></partial-response>')


class Respuesta:
    def __init__(self, contenido: str | bytes, cabeceras: dict | None = None):
        self.content = contenido.encode("utf-8") if isinstance(contenido, str) else contenido
        self.headers = cabeceras or {"content-type": "text/html;charset=UTF-8"}
        self.status_code = 200


class SitioFalso:
    """Imita el formulario: GET -> formulario; POST búsqueda -> resultados; POST AJAX -> parcial;
    POST con un control de fila -> PDF. Registra cada solicitud para las aserciones."""

    def __init__(self, filas: list[str]):
        self.todas, self.filas, self.pedidos = filas, filas, []

    def __call__(self, metodo, url, datos=None, cabeceras=None, reintentos=4, timeout=60):
        self.pedidos.append((metodo, url, dict(datos or {}), dict(cabeceras or {})))
        if metodo == "GET":
            return Respuesta(pagina_formulario())
        if "formaIngreso:fecini_input" in datos:
            # Como el sitio real: solo las filas con fecha dentro del rango pedido
            from datetime import datetime as _dt

            def f(t):
                return _dt.strptime(t, "%d/%m/%Y").date()

            ini, fin = f(datos["formaIngreso:fecini_input"]), f(datos["formaIngreso:fecfin_input"])
            self.filas = [r for r in self.todas
                          if ini <= f(re.search(r'<td role="gridcell">([^<]+)</td>', r).group(1)) <= fin]
            return Respuesta(pagina_resultados(self.filas, len(self.filas)))
        if datos.get("javax.faces.partial.ajax") == "true":
            primera = int(datos["formaIngreso:j_id_d_first"])
            n = int(datos["formaIngreso:j_id_d_rows"])
            return Respuesta(respuesta_parcial(self.filas[primera:primera + n]))
        control = next(k for k in datos if k.endswith(":j_id_p"))
        return Respuesta(b"%PDF-1.6 anexo de " + control.encode(), {
            "content-type": "application/pdf",
            "content-disposition": "attachment;filename=\"Fechas%20Exdividendo%20Grupo%20Bol%C3%ADvar%202026.pdf\"; "
                                   "filename*=UTF-8''Fechas%20Exdividendo%20Grupo%20Bol%C3%ADvar%202026.pdf",
        })
