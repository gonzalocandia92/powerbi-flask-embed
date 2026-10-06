"""Structured writer for ``FinalReport`` 1.3.1 (V1.5.1): typed evidence references + type-aware repair.

Shares the runtime / repair loop with every structured writer (``LLMStructuredReportWriter``) and overrides only its hooks.
What 1.3.1 adds over ``structured_writer_v13``:

* the model sees, per evidence record, facts and series as DIFFERENT lists, each with a ready-to-copy typed ``ref``;
* the prompt states which reference goes with which component (fact → card/hero, series → bar_chart,
  series_item → one row of a series as a card) with small worked examples;
* the writer does not copy numbers the backend already knows: cards with a ``value_ref`` and charts with a
  ``series_ref`` are materialized by the system (``visual_validation_v131``);
* ``metric_strip`` has no quota; ``methodology_notes`` may cite any evidence of the report;
* repair errors are type-aware and carry an inventory (a series used as a fact says "exists as SERIES");
  they are clipped much later than the generic 300 characters so the inventory survives.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from .composed_report import ComposedReportInput
from .composition_validation_v131 import validate_report_against_composition_v131
from .final_report_v131 import FinalReportV131, final_report_v131_json_schema
from .structured_writer import LLMStructuredReportWriter, _stamp_literal_notes
from .visual_content import MAX_CHART_ITEMS
from .visual_validation_v131 import materialize_visuals_v131
from .writer import MAX_VALIDATION_ERRORS, _strip_fences

STRUCTURED_WRITER_PROMPT_VERSION_131 = "structured-writer-v131"
MAX_PROMPT_FACTS = 20
MAX_ERROR_CHARS_131 = 1600     # evidence inventories must survive into the repair message

_RULES = """\
Sos el redactor de informes ejecutivos de KLARA. Recibís un ComposedReportInput (v2): una ESTRUCTURA ya decidida (items en orden) y la EVIDENCIA analítica autorizada para cada item, con su respuesta narrativa y, cuando existe, evidencia estructurada (facts y series). Tu única tarea es redactar el contenido de cada item a partir de esa evidencia y devolver un FinalReport 1.3.1 (JSON). Un renderizador determinístico se ocupa de todo lo visual: vos describís QUÉ se muestra, nunca CÓMO se ve.

LA ESTRUCTURA ES OBLIGATORIA
- "items" del input define exactamente qué componentes tiene el informe y en qué orden. Tu salida debe tener EXACTAMENTE un item por cada item del input, en el MISMO orden. Correspondencia de types: executive_summary -> "hero"; kpi_grid -> "metric_strip"; attention_points -> "attention_grid"; section, notes y methodology_notes conservan su type.
- No agregues, no elimines, no reordenes ni fusiones items, ni crees secciones nuevas. section: copiá key y title exactamente. notes: copiá text exactamente.
- Tu libertad editorial existe sólo DENTRO de cada item.

EVIDENCIA, PROVENANCE Y CIFRAS
- Cada item lista evidence_keys (y, en sections, supplementary_keys). Sólo podés usar la evidencia de ese item. Todo componente analítico declara source_section_keys con keys copiadas literalmente de la evidencia AUTORIZADA para ese item y con status "ok".
- Usá exclusivamente la evidencia recibida. No inventes números, fechas, sucursales, causas ni conocimiento externo. No redondees, no recalcules, no derives sumas, diferencias ni porcentajes. Toda cifra de un componente estructurado debe existir en la evidencia autorizada.
- Evidencia con status "failed" o "missing" no tiene contenido: nunca la uses ni inventes sus cifras. Una section sin evidencia ok debe tener status "unavailable", summary breve y neutral, blocks y secondary_blocks vacíos.
- No insinúes causalidad que la evidencia no demuestre. Si dos métricas tienen bases o períodos distintos, presentalas por separado y aclaralo en una annotation. Traducí los semantic_notes a lenguaje de negocio (sin "grano", "routing", "skill", "schema").

REFERENCIAS TIPADAS A LA EVIDENCIA ESTRUCTURADA (tres tipos distintos; no se pueden mezclar)
Cada registro de "evidence" trae "facts" (escalares) y "series" (listas con una dimensión), cada uno con su "ref" lista para copiar.
- fact: UN valor escalar (p. ej. "Ventas totales"). Ref: {"type":"fact","section_key":<key del análisis>,"fact_key":<key del fact>}. Úsala en tarjetas (value_ref) y en el hero.highlight.
- series: una lista completa (p. ej. "Ventas por sucursal"). Ref: {"type":"series","section_key":...,"series_key":...}. Úsala SOLO como series_ref de un bar_chart.
- series_item: UN elemento de una serie (p. ej. la sucursal "Ayacucho"). Ref: {"type":"series_item","section_key":...,"series_key":...,"label":<label EXACTO tal como figura en la serie>}. Úsala en una tarjeta (value_ref) o en el hero.highlight cuando una fila puntual sea realmente significativa.
- Una key de series NO es un fact y una de facts NO es una serie: si usás el tipo equivocado el informe se rechaza. No existe otro tipo de referencia.
- Ejemplo. Facts: total_ventas. Series: ventas_by_sucursal (San Martin, Italia).
  "Ventas totales" -> value_ref {"type":"fact",...,"fact_key":"total_ventas"}.
  "Todas las sucursales" -> bar_chart con series_ref {"type":"series",...,"series_key":"ventas_by_sucursal"}.
  "San Martin" -> value_ref {"type":"series_item",...,"series_key":"ventas_by_sucursal","label":"San Martin"}.
- Con value_ref NO escribas value: el sistema lo completa con el valor real de la evidencia (y secondary_value con secondary_value_ref, p. ej. la variación de la misma sucursal desde otra serie, mismo label). Sin ref, value debe ser una cifra literal de la evidencia autorizada. El section_key de cada ref debe figurar en source_section_keys del componente.
- Si una evidencia no tiene facts/series, sólo podés usar cifras que aparezcan literalmente en su "answer", y no obligues un visual: usala como paragraph, bullet_list, summary o annotation.

COMPONENTES (vocabulario cerrado; no existe ningún otro)
- hero (para executive_summary): headline editorial (una sola idea basada en la evidencia; no afirmes lo contrario de lo que muestran los datos), deck, metadata (chips cortos: período, comparación, moneda; sólo lo que surja de la evidencia), highlight opcional {label, supporting_text, value_ref, source_section_keys} con UNA cifra destacada, highlights (3 a 5 bullets con source_section_keys) y headline_source_section_keys.
- metric_strip (para kpi_grid): 0 a max_kpis metrics {key, label, value_ref, secondary_value_ref, trend, impact, source_section_keys}. max_kpis es un TOPE, no una cuota: preferí 3 KPIs excelentes a 8 forzados. Prioridad: (1) facts fuertes y ejecutivos; (2) series_item sólo si una fila concreta es especialmente significativa; (3) si no hay una métrica clara, no la crees (metrics puede ser []). trend es la dirección ("up","down","stable","neutral") y debe coincidir con el signo de la evidencia si es una variación; impact indica si es favorable ("positive","negative","neutral").
- section: key, title, status, summary, layout, bloques y source_section_keys (las keys de las evidencias que respaldan la sección; si lo omitís, el sistema lo completa con las de sus bloques).
  layout "standard": una columna (blocks). "feature": contenido principal en blocks + columna de apoyo en secondary_blocks. "split": dos columnas equivalentes. En "standard" secondary_blocks debe estar vacío. No hay otros layouts ni anchos ni colores.
  Bloques: paragraph {text}; bullet_list {items}; table {caption, columns, rows} (celdas de texto; las cifras deben existir en la evidencia); callout {severity info|warning|critical, title, text}; pull_quote {text, stat opcional (una tarjeta con value_ref), source_section_keys}; annotation {text, source_section_keys} para aclaraciones metodológicas o caveats junto al dato; metric_cards {cards:[{label, value_ref, secondary_value_ref, supporting_text, source_section_keys}]} (máximo 6); bar_chart {variant, title, series_ref, secondary_series_ref opcional, secondary_label, items, source_section_keys}.
  bar_chart: variant "bars" (proporcional al máximo), "ranking" (lista numerada con columna secundaria opcional, p. ej. variación junto a participación; secondary_series_ref debe ser una serie con las mismas etiquetas) o "distribution" (participación sobre el total mostrado). series_ref es un SeriesRef. DEJÁ items VACÍO ([]): el sistema lo completa con los valores exactos de la serie. Máximo 3 bar_chart por sección.
- attention_grid (para attention_points): points {severity "high"|"medium"|"low", title, text, source_section_keys}. La severidad es criterio editorial pero no puede cambiar los hechos. Puede quedar vacío.
- methodology_notes: notes {kind "methodology"|"data_quality"|"general", text, source_section_keys}. Puede citar CUALQUIER evidencia del informe, pero sólo para metodología, calidad de datos, comparabilidad, alcance, limitaciones o análisis no disponible (p. ej. ventas de líneas vs. de cabecera, pagos que no concilian con ventas, períodos o bases distintos que figuren en las respuestas o en los semantic_notes). No repitas hallazgos comerciales normales. Puede quedar vacío.

DATA STORYTELLING
- Gráficos y tarjetas NO son obligatorios: usalos cuando aporten. Si la narrativa alcanza, usá texto.
- Patrón recomendado en una sección: insight principal (summary / pull_quote) -> evidencia cuantitativa (bar_chart o metric_cards) -> explicación (paragraph) -> caveat cerca del dato (annotation).
- hero: no repitas todas las secciones. attention_grid: sólo hallazgos que realmente merezcan atención. No repitas la misma información en summary, paragraph, tablas y bullets.
- period: completá sólo si surge explícitamente de la evidencia; si no, null.

PRESENTACIÓN
- Español rioplatense neutro, profesional y conciso, en texto plano. Prohibido HTML, Markdown, CSS, SVG, colores, tamaños, clases, URLs o decisiones de diseño.
- No expongas DAX, consultas, skills, routing, tokens, modelos ni identificadores técnicos. composition_warnings es información interna: no la reproduzcas.
- Respondé únicamente con un objeto JSON válido que cumpla el schema (schema_version "1.3.1"). Sin texto adicional ni bloques de código.
"""


def build_system_prompt() -> str:
    schema = json.dumps(final_report_v131_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"{_RULES}\nSCHEMA JSON DE SALIDA (FinalReport, schema_version \"1.3.1\"):\n{schema}\n"


def build_user_message(composed: ComposedReportInput) -> str:
    """The composed input with an explicit evidence inventory: facts and series apart, each with a copyable typed ref.

    Raw ``tables`` stay out (persisted for audit / regeneration); sizes are bounded to the prompt budget.
    """
    payload = composed.model_dump(mode="json", exclude_none=True)
    for index, item in enumerate(payload["items"]):
        item["index"] = index
    for record in payload["evidence"]:
        record.pop("tables", None)
        section_key = record["key"]
        record["facts"] = [
            {"key": fact["key"], "label": fact["label"], "formatted_value": fact["formatted_value"],
             **({"kind": fact["kind"]} if fact.get("kind") == "comparison" else {}),
             "ref": {"type": "fact", "section_key": section_key, "fact_key": fact["key"]}}
            for fact in record.get("facts", [])[:MAX_PROMPT_FACTS]]
        record["series"] = [
            {"key": series["key"], "title": series["title"], "label_field": series["label_field"],
             "value_field": series["value_field"], "total_items": series["total_items"],
             "ref": {"type": "series", "section_key": section_key, "series_key": series["key"]},
             "items": [{"label": i["label"], "formatted_value": i["formatted_value"]}
                       for i in series["items"][:MAX_CHART_ITEMS]]}
            for series in record.get("series", [])]
        if not record["facts"]:
            record.pop("facts")
        if not record["series"]:
            record.pop("series")
    return json.dumps(payload, ensure_ascii=False)


def _clip(value: str) -> str:
    return value if len(value) <= MAX_ERROR_CHARS_131 else value[:MAX_ERROR_CHARS_131] + "…"


def parse_structured_report_v131(text: str, composed: ComposedReportInput) -> tuple[FinalReportV131 | None, list[str]]:
    """Parse -> stamp literal notes -> materialize (cards, chart items) -> schema -> composition + figures."""
    try:
        data = json.loads(_strip_fences(text))
    except (ValueError, TypeError) as exc:
        return None, [_clip(f"JSON inválido: {exc}")]
    if not isinstance(data, dict):
        return None, ["La salida debe ser un objeto JSON."]
    _stamp_literal_notes(data, composed)
    errors = [_clip(error) for error in materialize_visuals_v131(data, composed)]
    if errors:
        return None, errors[:MAX_VALIDATION_ERRORS]
    try:
        report = FinalReportV131.model_validate(data)
    except ValidationError as exc:
        details = []
        for item in exc.errors(include_input=False, include_url=False)[:MAX_VALIDATION_ERRORS]:
            path = ".".join(str(part) for part in item.get("loc", ())) or "(raíz)"
            details.append(_clip(f"{path}: {item.get('msg')}"))
        return None, details
    errors = [_clip(error) for error in validate_report_against_composition_v131(report, composed)]
    return (None, errors) if errors else (report, [])


class LLMStructuredReportWriterV131(LLMStructuredReportWriter):
    report_schema_version = "1.3.1"
    prompt_version = STRUCTURED_WRITER_PROMPT_VERSION_131

    def _build_system_prompt(self) -> str:
        return build_system_prompt()

    def _build_user_message(self, composed: ComposedReportInput) -> str:
        return build_user_message(composed)

    def _parse(self, text: str, composed: ComposedReportInput):
        return parse_structured_report_v131(text, composed)
