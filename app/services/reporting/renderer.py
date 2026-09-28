"""Minimal public Markdown view over an analytical draft."""
from __future__ import annotations

from .contracts import ReportDraft


def render_markdown(draft: ReportDraft) -> str:
    parts = [f"# {draft.name}"]
    for section in draft.sections:
        body = "> No se pudo generar esta sección." if section.had_error else section.answer
        parts.append(f"## {section.title}\n\n{body}")
    return "\n\n".join(parts) + "\n"
