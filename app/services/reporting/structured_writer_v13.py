"""Structured writer for ``FinalReport`` 1.3 (V1.5 data story): ``ComposedReportInput`` v2 -> ``FinalReportV13``.

Shares the whole runtime / repair loop with the 1.2 writer (``LLMStructuredReportWriter``): this class only provides
its own prompt, its own input rendering and its own parse+validate step. Nothing in the 1.2 writer branches on a
schema version.

Freedom is limited to the INSIDE of each item: wording, the hero's highlighted figure, which metrics, which section
``layout``, which blocks (paragraph, bullet_list, table, callout, pull_quote, bar_chart, metric_cards, annotation) and
in which slot, which attention points and their severity. WHAT items exist and in WHAT ORDER stays fixed by the
composed input and is enforced after the model answers; every figure it shows is checked against the authorised
evidence (``visual_validation``). The model never writes HTML, CSS, SVG, colors or sizes.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from .composed_report import ComposedReportInput
from .composition_validation_v13 import validate_report_against_composition_v13
from .final_report_v13 import FinalReportV13, final_report_v13_json_schema
from .structured_writer import LLMStructuredReportWriter, _stamp_literal_notes
from .visual_content import MAX_CHART_ITEMS
from .visual_validation import materialize_visuals
from .writer import MAX_VALIDATION_ERRORS, _clip, _strip_fences

STRUCTURED_WRITER_PROMPT_VERSION_13 = "structured-writer-v13"
MAX_PROMPT_FACTS = 20

_RULES = """\
Sos el redactor de informes ejecutivos de KLARA. Recibís un ComposedReportInput (v2): una ESTRUCTURA ya decidida (items en orden) y la EVIDENCIA analítica autorizada para cada item, con su respuesta narrativa y, cuando existe, evidencia estructurada (facts y series). Tu única tarea es redactar el contenido de cada item a partir de esa evidencia y devolver un FinalReport 1.3 (JSON). Un renderizador determinístico se ocupa de todo lo visual: vos describís QUÉ se muestra, nunca CÓMO se ve.

LA ESTRUCTURA ES OBLIGATORIA
- "items" del input define exactamente qué componentes tiene el informe y en qué orden. Tu salida debe tener EXACTAMENTE un item por cada item del input, en el MISMO orden. Correspondencia de types: executive_summary -> "hero"; kpi_grid -> "metric_strip"; attention_points -> "attention_grid"; section, notes y methodology_notes conservan su type.
- No agregues, no elimines, no reordenes ni fusiones items, ni crees secciones nuevas. section: copiá key y title exactamente. notes: copiá text exactamente.
- Tu libertad editorial existe sólo DENTRO de cada item.

EVIDENCIA, PROVENANCE Y CIFRAS
- Cada item lista evidence_keys (y, en sections, supplementary_keys). Sólo podés usar la evidencia de ese item. Toda afirmación o componente analítico declara source_section_keys con keys copiadas literalmente de la evidencia AUTORIZADA para ese item y con status "ok".
- Usá exclusivamente la evidencia recibida. No inventes números, fechas, sucursales, causas ni conocimiento externo. No redondees, no recalcules, no derives sumas, diferencias ni porcentajes. Toda cifra que muestres en un componente estructurado debe existir en la evidencia autorizada: si no está, no la muestres.
- Referencias a evidencia estructurada: {"section_key": <key del análisis>, "key": <key del fact o de la serie dentro de ese análisis>}. Usá sólo las que aparecen en "facts" y "series" de la evidencia autorizada del item. El section_key de cada referencia debe figurar también en source_section_keys del componente.
- Si una evidencia no tiene facts/series, sólo podés usar cifras que aparezcan literalmente en su "answer".
- Evidencia con status "failed" o "missing" no tiene contenido: nunca la uses ni inventes sus cifras. Una section sin ninguna evidencia ok debe tener status "unavailable", summary breve y neutral, blocks y secondary_blocks vacíos.
- No insinúes causalidad que la evidencia no demuestre. Si dos métricas tienen bases o períodos distintos, presentalas por separado y aclaralo en una annotation. Traducí los semantic_notes a lenguaje de negocio (sin "grano", "routing", "skill", "schema").

COMPONENTES (vocabulario cerrado; no existe ningún otro)
- hero (para executive_summary): headline editorial (una sola idea, basada en la evidencia; no afirmes lo contrario de lo que muestran los datos), deck (1-2 frases), metadata (chips cortos: período, comparación, moneda; sólo lo que surja de la evidencia), highlight opcional {value, label, supporting_text, fact_ref, source_section_keys} con UNA cifra destacada (value = formatted_value del fact), highlights (3 a 5 bullets con source_section_keys) y headline_source_section_keys.
- metric_strip (para kpi_grid): hasta max_kpis metrics {key, label, value, secondary_value, trend, impact, fact_ref, secondary_fact_ref, source_section_keys}. Con fact_ref, value debe ser EXACTAMENTE el formatted_value de ese fact. trend es la dirección ("up","down","stable","neutral") y debe coincidir con el signo del fact si es una comparación; impact indica si es favorable ("positive","negative","neutral"). Sólo evidencia de ese kpi_grid.
- section: title, summary, layout y bloques.
  layout "standard": una columna (blocks). "feature": contenido principal en blocks + columna de apoyo en secondary_blocks (tarjetas, annotation, callout). "split": dos columnas equivalentes, blocks y secondary_blocks. En "standard" secondary_blocks debe estar vacío. Usá feature o split sólo si aportan; no hay otras opciones de layout, ni anchos, ni colores.
  Bloques: paragraph {text}; bullet_list {items}; table {caption, columns, rows} (celdas de texto; las cifras deben existir en la evidencia); callout {severity info|warning|critical, title, text}; pull_quote {text, stat opcional (una tarjeta con su fact_ref), source_section_keys} para la frase clave de la sección; annotation {text, source_section_keys} para aclaraciones metodológicas o caveats junto al dato; metric_cards {cards:[{label, value, supporting_text, fact_ref, source_section_keys}]} para tarjetas de apoyo; bar_chart {variant, title, series_ref, secondary_series_ref opcional, secondary_label, items, source_section_keys}.
  bar_chart: variant "bars" (barras proporcionales al máximo), "ranking" (lista numerada con columna secundaria opcional, p. ej. variación junto a participación) o "distribution" (participación sobre el total mostrado). series_ref apunta a una serie existente. DEJÁ items VACÍO ([]): el sistema lo completa con los valores exactos de la serie. Si listás items, deben ser copia exacta de la serie (label, value, formatted_value). No inventes series ni cifras. Máximo 3 bar_chart por sección.
- attention_grid (para attention_points): points {severity "high"|"medium"|"low", title, text, source_section_keys}. La severidad es tu criterio editorial pero no puede cambiar los hechos. Puede quedar vacío.
- methodology_notes: notes {kind "methodology"|"data_quality"|"general", text, source_section_keys}.

DATA STORYTELLING
- Los gráficos y tarjetas NO son obligatorios: usalos cuando aporten (rankings, distribuciones, comparaciones). Si la narrativa alcanza, usá texto.
- Patrón recomendado en una sección: insight principal (summary / pull_quote) -> evidencia cuantitativa (bar_chart o metric_cards) -> explicación (paragraph) -> contexto o caveat cerca del dato (annotation). Los caveats metodológicos van junto al dato que matizan, no sólo al final.
- executive_summary/hero: no repitas todas las secciones. attention_grid: sólo hallazgos que realmente merezcan atención; no conviertas diferencias metodológicas normales en puntos de atención.
- No repitas la misma información en summary, paragraph, tablas y bullets. Preferí menos componentes útiles.
- period: completá sólo si surge explícitamente de la evidencia; si no, null.

PRESENTACIÓN
- Español rioplatense neutro, profesional y conciso, en texto plano. Prohibido HTML, Markdown, CSS, SVG, colores, tamaños, clases, URLs o decisiones de diseño.
- No expongas DAX, consultas, skills, routing, tokens, modelos ni identificadores técnicos. composition_warnings es información interna: no la reproduzcas.
- Respondé únicamente con un objeto JSON válido que cumpla el schema (schema_version "1.3"). Sin texto adicional ni bloques de código.
"""


def build_system_prompt() -> str:
    schema = json.dumps(final_report_v13_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"{_RULES}\nSCHEMA JSON DE SALIDA (FinalReport, schema_version \"1.3\"):\n{schema}\n"


def build_user_message(composed: ComposedReportInput) -> str:
    """The composed input, minus what the writer cannot use: raw ``tables`` (persisted for audit / regeneration,
    but table blocks are written from facts/series) and anything beyond the prompt budget."""
    payload = composed.model_dump(mode="json", exclude_none=True)
    for index, item in enumerate(payload["items"]):
        item["index"] = index
    for record in payload["evidence"]:
        record.pop("tables", None)
        if "facts" in record:
            record["facts"] = [_fact_view(fact) for fact in record["facts"][:MAX_PROMPT_FACTS]]
        for series in record.get("series", []):
            series["items"] = series["items"][:MAX_CHART_ITEMS]
    return json.dumps(payload, ensure_ascii=False)


def _fact_view(fact: dict) -> dict:
    return {key: fact[key] for key in ("key", "label", "value", "formatted_value", "kind", "comparison_label")
            if key in fact}


def parse_structured_report_v13(text: str, composed: ComposedReportInput) -> tuple[FinalReportV13 | None, list[str]]:
    """Parse -> stamp literal notes -> materialize chart items -> schema-validate -> enforce composition + figures."""
    try:
        data = json.loads(_strip_fences(text))
    except (ValueError, TypeError) as exc:
        return None, [_clip(f"JSON inválido: {exc}")]
    if not isinstance(data, dict):
        return None, ["La salida debe ser un objeto JSON."]
    _stamp_literal_notes(data, composed)
    errors = [_clip(error) for error in materialize_visuals(data, composed)]
    if errors:
        return None, errors[:MAX_VALIDATION_ERRORS]
    try:
        report = FinalReportV13.model_validate(data)
    except ValidationError as exc:
        details = []
        for item in exc.errors(include_input=False, include_url=False)[:MAX_VALIDATION_ERRORS]:
            path = ".".join(str(part) for part in item.get("loc", ())) or "(raíz)"
            details.append(_clip(f"{path}: {item.get('msg')}"))
        return None, details
    errors = [_clip(error) for error in validate_report_against_composition_v13(report, composed)]
    return (None, errors) if errors else (report, [])


class LLMStructuredReportWriterV13(LLMStructuredReportWriter):
    report_schema_version = "1.3"
    prompt_version = STRUCTURED_WRITER_PROMPT_VERSION_13

    def _build_system_prompt(self) -> str:
        return build_system_prompt()

    def _build_user_message(self, composed: ComposedReportInput) -> str:
        return build_user_message(composed)

    def _parse(self, text: str, composed: ComposedReportInput):
        return parse_structured_report_v13(text, composed)
