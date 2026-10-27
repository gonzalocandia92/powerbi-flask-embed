"""``FallbackReportBuilder``: ``ComposedReportInput`` -> a valid ``FinalReportV131`` without any model (V1.5.1).

When the editorial writer cannot satisfy the FinalReport contract (invalid output, still invalid after its repair), the
paid analyses must not be wasted. This builder produces a plain, legible, honest document from what the composer
already authorised:

    executive_summary  neutral hero: the report name, no highlight, no figure
    kpi_grid           empty metric strip (no automatic KPIs)
    section            title/key exactly as composed, layout ``standard``; the ORIGINAL analytics answers as paragraphs
                       (primary evidence first, then "Análisis complementario" for coordinator evidence, in the SAME
                       section); a neutral note when part of the evidence was unavailable
    attention_points   empty grid
    notes              the user's literal text
    methodology_notes  the semantic notes of the evidence (verbatim) + a neutral note per unavailable analysis

It respects the macro structure exactly (same items, same order, same keys). It never rewrites or computes a figure, never
builds charts or cards, and has no LLM, DB, Power BI or renderer dependency: it is a pure function of its input and
testable with a ``ComposedReportInput`` alone. It refuses (``FallbackUnavailableError``) when there is no successful
evidence: fallback hides an editorial failure, never a domain one.
"""
from __future__ import annotations

from .composed_report import ComposedReportInput, EvidenceRecord
from .composition_validation_v131 import validate_report_against_composition_v131
from .final_report_v131 import FinalReportV131
from .report_content import MAX_ITEMS

MAX_TEXT = 3_900
MAX_FALLBACK_BLOCKS = 20
NEUTRAL_UNAVAILABLE = "El análisis de esta sección no estuvo disponible."
NEUTRAL_PARTIAL = "Parte del análisis de esta sección no estuvo disponible."
SUPPLEMENTARY_LABEL = "Análisis complementario"


class FallbackUnavailableError(Exception):
    """No safe deterministic report can be built from this input (e.g. no successful evidence)."""


def _chunks(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on blank lines, then hard-cut anything still longer than ``limit`` (never drops content)."""
    out: list[str] = []
    current = ""
    for part in (p.strip() for p in text.replace("\r\n", "\n").split("\n\n")):
        if not part:
            continue
        while len(part) > limit:
            cut = part.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                out.append(current)
                current = ""
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if current and len(current) + 2 + len(part) > limit:
            out.append(current)
            current = ""
        current = f"{current}\n\n{part}" if current else part
    if current:
        out.append(current)
    return out


class FallbackReportBuilder:
    def build(self, composed: ComposedReportInput) -> FinalReportV131:
        if not composed.has_successful_evidence():
            raise FallbackUnavailableError("No hay análisis exitosos para construir un informe.")
        records = composed.evidence_by_key()
        items = [self._item(composed, index, want, records) for index, want in enumerate(composed.items)]
        title = composed.report_name.strip()[:300] or "Informe"
        try:
            report = FinalReportV131.model_validate({"schema_version": "1.3.1", "title": title, "items": items})
        except ValueError as exc:
            raise FallbackUnavailableError(f"El informe de contingencia no es válido: {exc}") from exc
        # Same enforcement as the writer's output. Markup is not checked: the text is the analytics answers verbatim
        # and the renderer escapes every string.
        problems = validate_report_against_composition_v131(report, composed, check_markup=False)
        if problems:
            raise FallbackUnavailableError("El informe de contingencia no respeta la estructura: " + "; ".join(problems[:3]))
        return report

    # -- per item ------------------------------------------------------------------------
    def _item(self, composed: ComposedReportInput, index: int, want, records: dict[str, EvidenceRecord]) -> dict:
        if want.type == "executive_summary":
            return {"type": "hero", "headline": composed.report_name.strip()[:300] or "Informe",
                    "deck": "Resumen de los análisis completados para este informe."}
        if want.type == "kpi_grid":
            return {"type": "metric_strip", "metrics": []}
        if want.type == "attention_points":
            return {"type": "attention_grid", "points": []}
        if want.type == "notes":
            return {"type": "notes", "text": want.text}
        if want.type == "methodology_notes":
            return {"type": "methodology_notes", "notes": self._notes(want, records)}
        return self._section(composed, index, want, records)

    def _section(self, composed: ComposedReportInput, index: int, want, records) -> dict:
        ok = composed.item_ok_keys(index)
        primary = [k for k in want.evidence_keys if k in ok]
        supplementary = [k for k in want.supplementary_keys if k in ok]
        if not ok:
            return {"type": "section", "key": want.key, "title": want.title, "status": "unavailable",
                    "summary": NEUTRAL_UNAVAILABLE, "source_section_keys": []}
        blocks: list[dict] = []
        for key in primary:
            blocks += [{"type": "paragraph", "text": t} for t in _chunks(records[key].answer or "")]
        for key in supplementary:
            parts = _chunks(records[key].answer or "")
            if parts:
                parts[0] = f"{SUPPLEMENTARY_LABEL}: {parts[0]}"
                parts = _chunks("\n\n".join(parts))
            blocks += [{"type": "paragraph", "text": t} for t in parts]
        if composed.item_unavailable_keys(index):
            blocks.append({"type": "annotation", "text": NEUTRAL_PARTIAL,
                           "source_section_keys": []})
        summary = blocks.pop(0)["text"] if blocks and blocks[0]["type"] == "paragraph" else NEUTRAL_UNAVAILABLE
        if len(blocks) > MAX_FALLBACK_BLOCKS:      # never cut silently: say so
            blocks = blocks[:MAX_FALLBACK_BLOCKS - 1] + [{
                "type": "annotation", "source_section_keys": [],
                "text": "Se omitió parte del contenido de esta sección por su extensión."}]
        return {"type": "section", "key": want.key, "title": want.title, "status": "ok", "layout": "standard",
                "summary": summary, "blocks": blocks,
                "source_section_keys": primary + supplementary}

    def _notes(self, want, records) -> list[dict]:
        notes: list[dict] = []
        for key in want.evidence_keys:
            record = records.get(key)
            if record is None:
                continue
            if record.status == "ok":
                for text in record.semantic_notes:
                    notes.append({"kind": "methodology", "text": text[:MAX_TEXT], "source_section_keys": [key]})
            else:
                label = record.title or key
                notes.append({"kind": "data_quality", "source_section_keys": [key],
                              "text": f"El análisis «{label}» no estuvo disponible para este informe."[:MAX_TEXT]})
        return notes[:MAX_ITEMS]
