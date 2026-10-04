"""Deterministic HTML rendering of a ``FinalReportV12`` (renderer ``html-v2``).

Consumes only ``FinalReportV12``: no LLM, no ``ComposedReportInput``, no ``ReportDraft``, no structure spec.
The order of ``report.items`` is the order of the document: this renderer takes NO editorial decisions (it never
hoists KPIs, never forces a summary first, never groups sections). All model-authored text is HTML-escaped; CSS
classes come from fixed semantic maps, never from report content; restrictive CSP, no JS.

Visual design is the one of ``html-v1`` (so reports look the same), duplicated here on purpose: ``html-v1`` is
frozen for historical reports and must never be coupled to the evolution of this renderer.
"""
from __future__ import annotations

from html import escape

from .final_report_v12 import (
    AttentionPointsItem12, ExecutiveSummaryItem12, FinalReportV12, KpiGridItem12, MethodologyNotesItem12,
    NotesItem12, ReportItem12, SectionItem12,
)
from .report_content import BulletListBlock, CalloutBlock, ParagraphBlock, ReportBlock, TableBlock
from .versions import FINAL_REPORT_SCHEMA_1_2, HTML_RENDERER_V2

# trend controls direction only (glyph + accessible label); it never implies a
# color. impact controls the color/semantic style; it never implies a direction.
_TREND_GLYPH = {
    "up": ("▲", "en alza"),
    "down": ("▼", "en baja"),
    "stable": ("▬", "estable"),
    "neutral": ("●", "sin tendencia"),
}
_IMPACT_STYLE = {
    "positive": ("impact-positive", "favorable"),
    "negative": ("impact-negative", "desfavorable"),
    "neutral": ("impact-neutral", "neutro"),
}
_ATTENTION = {"high": ("attn-high", "Alta"), "medium": ("attn-medium", "Media"), "low": ("attn-low", "Baja")}
_CALLOUT = {"info": "callout-info", "warning": "callout-warning", "critical": "callout-critical"}
_SHORT_TABLE_ROWS = 15  # print only: tables up to this many rows are kept on one page
_NOTE_TITLES = {"methodology": "Metodología", "data_quality": "Calidad de datos", "general": "Nota"}

_CSS = """
:root{--bg:#f1f3f5;--paper:#fff;--ink:#1f2933;--muted:#616e7c;--line:#e4e7eb;--accent:#1f4e79;
--pos:#0f7b4f;--pos-bg:#e6f4ee;--neg:#b42318;--neg-bg:#fdecea;--neu:#52606d;--neu-bg:#eef1f4;
--warn:#8a5a00;--warn-bg:#fff5db;--info:#1f4e79;--info-bg:#e8f0f8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.doc{max-width:1040px;margin:32px auto;background:var(--paper);border:1px solid var(--line);border-radius:6px;padding:56px 64px}
header{border-bottom:2px solid var(--accent);padding-bottom:20px;margin-bottom:32px}
h1{font-size:2rem;line-height:1.2;margin:0 0 6px;color:var(--accent)}
.subtitle{font-size:1.1rem;color:var(--muted);margin:0}
.period{margin-top:14px;font-size:.9rem;color:var(--muted)}
.period span+span::before{content:"·";margin:0 8px}
h2{font-size:1.35rem;margin:40px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:1.05rem;margin:0 0 6px}
p{margin:0 0 12px}
.headline{font-size:1.2rem;font-weight:600;margin-bottom:12px}
ul{margin:0 0 12px;padding-left:22px}li{margin-bottom:6px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin:20px 0}
.kpi{border:1px solid var(--line);border-radius:6px;padding:14px 16px;background:#fafbfc}
.kpi-label{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.kpi-value{font-size:1.6rem;font-weight:700;line-height:1.3}
.kpi-secondary{font-size:.85rem;color:var(--muted)}
.trend{display:inline-block;margin-top:4px;padding:1px 8px;border-radius:10px;font-size:.78rem;font-weight:600}
.impact-positive{color:var(--pos);background:var(--pos-bg)}.impact-negative{color:var(--neg);background:var(--neg-bg)}
.impact-neutral{color:var(--neu);background:var(--neu-bg)}
.section-summary{color:var(--ink);margin-bottom:14px}
.table-wrap{overflow-x:auto;margin:0 0 16px}
table{border-collapse:collapse;width:100%;font-size:.92rem}
caption{caption-side:top;text-align:left;font-size:.85rem;color:var(--muted);padding-bottom:6px}
th,td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
thead th{background:#f5f7f9;font-weight:600;border-bottom:2px solid var(--line)}
tbody tr:nth-child(even){background:#fafbfc}
.callout{border-left:4px solid;border-radius:4px;padding:12px 16px;margin:0 0 16px}
.callout-title{font-weight:600;margin-bottom:2px}
.callout-info{border-color:var(--info);background:var(--info-bg)}
.callout-warning{border-color:var(--warn);background:var(--warn-bg)}
.callout-critical{border-color:var(--neg);background:var(--neg-bg)}
.attention{border:1px solid var(--line);border-left:4px solid;border-radius:4px;padding:12px 16px;margin-bottom:12px}
.attn-high{border-left-color:var(--neg)}.attn-medium{border-left-color:var(--warn)}.attn-low{border-left-color:var(--info)}
.badge{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.notes{font-size:.9rem;color:var(--muted)}.notes h2{color:var(--ink)}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);font-size:.8rem;color:var(--muted)}
@media (max-width:720px){.doc{margin:0;border:0;border-radius:0;padding:28px 20px}h1{font-size:1.5rem}}
@page{size:A4;margin:15mm}
@media print{
body{background:#fff;font-size:11pt;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.doc{max-width:none;margin:0;border:0;border-radius:0;padding:0}
h1{font-size:2rem}
h2,h3,caption{break-after:avoid}
.table-wrap{overflow:visible}
/* Short tables stay whole; long ones may split between rows (header repeats) instead of leaving a page gap. */
.kpi,.attention,.callout,.table-short,tr{break-inside:avoid}
thead{display:table-header-group}
footer{break-inside:avoid}}
"""


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _paragraphs(text: str) -> str:
    parts = [chunk.strip() for chunk in text.replace("\r\n", "\n").split("\n\n") if chunk.strip()]
    return "".join(f"<p>{_e(part).replace(chr(10), '<br>')}</p>" for part in parts)


class HtmlReportRendererV2:
    """``FinalReportV12`` -> standalone HTML document (string). Deterministic and DB-free.

    Its identity is persisted next to every HTML it produces (``renderer_version``): a change that alters
    existing output means a NEW renderer version, never an edit of this one in place.
    """

    renderer_version = HTML_RENDERER_V2
    supported_schema_versions = (FINAL_REPORT_SCHEMA_1_2,)

    def render(self, report: FinalReportV12) -> str:
        section_numbers = iter(range(1, len(report.items) + 1))
        body = [self._header(report)]
        for item in report.items:  # the JSON order is the authority
            body.append(self._item(item, section_numbers))
        body.append("<footer>Informe generado automáticamente por KLARA a partir de datos analíticos verificados.</footer>")
        return (
            "<!DOCTYPE html>\n<html lang=\"es\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
            f"<title>{_e(report.title)}</title><style>{_CSS}</style></head>"
            f"<body><main class=\"doc\">{''.join(body)}</main></body></html>\n"
        )

    # -- dispatch: one renderer per item type; nothing here reorders or filters items -------------------
    def _item(self, item: ReportItem12, section_numbers) -> str:
        if isinstance(item, ExecutiveSummaryItem12):
            return self._executive_summary(item)
        if isinstance(item, KpiGridItem12):
            return self._kpi_grid(item)
        if isinstance(item, SectionItem12):
            return self._section(item, next(section_numbers))
        if isinstance(item, AttentionPointsItem12):
            return self._attention_points(item)
        if isinstance(item, NotesItem12):
            return self._notes(item)
        if isinstance(item, MethodologyNotesItem12):
            return self._methodology_notes(item)
        return ""  # unknown item types are never rendered

    # -- parts -----------------------------------------------------------------
    def _header(self, report: FinalReportV12) -> str:
        period = report.period
        bits = []
        if period.label:
            bits.append(f"<span>{_e(period.label)}</span>")
        if period.start or period.end:
            bits.append(f"<span>{_e(period.start or '…')} – {_e(period.end or '…')}</span>")
        if period.comparison_label:
            bits.append(f"<span>Comparado con {_e(period.comparison_label)}</span>")
        subtitle = f"<p class=\"subtitle\">{_e(report.subtitle)}</p>" if report.subtitle else ""
        period_html = f"<div class=\"period\">{''.join(bits)}</div>" if bits else ""
        return f"<header><h1>{_e(report.title)}</h1>{subtitle}{period_html}</header>"

    def _executive_summary(self, summary: ExecutiveSummaryItem12) -> str:
        # Provenance (source_section_keys) is admin/debug data, kept out of the client HTML.
        items = "".join(f"<li>{_e(item.text)}</li>" for item in summary.highlights)
        listing = f"<ul>{items}</ul>" if items else ""
        return f"<section><h2>Resumen ejecutivo</h2><p class=\"headline\">{_e(summary.headline)}</p>{listing}</section>"

    def _kpi_grid(self, grid: KpiGridItem12) -> str:
        if not grid.kpis:
            return ""
        cards = []
        for kpi in grid.kpis:
            secondary = f"<div class=\"kpi-secondary\">{_e(kpi.secondary_value)}</div>" if kpi.secondary_value else ""
            trend = ""
            if kpi.trend:
                glyph, direction_label = _TREND_GLYPH[kpi.trend]
                impact_css, _impact_label = _IMPACT_STYLE[kpi.impact or "neutral"]
                trend = (f"<span class=\"trend {impact_css}\" title=\"{_e(direction_label)}\">"
                         f"<span aria-hidden=\"true\">{glyph}</span> {_e(direction_label)}</span>")
            cards.append(f"<div class=\"kpi\"><div class=\"kpi-label\">{_e(kpi.label)}</div>"
                         f"<div class=\"kpi-value\">{_e(kpi.value)}</div>{secondary}{trend}</div>")
        return f"<section class=\"kpis\" aria-label=\"Indicadores destacados\">{''.join(cards)}</section>"

    def _section(self, section: SectionItem12, number: int) -> str:
        if section.status == "unavailable":
            return (f"<section id=\"section-{number}\"><h2>{_e(section.title)}</h2>"
                    f"<div class=\"callout callout-info\" role=\"note\">"
                    f"<div class=\"callout-title\">Análisis no disponible</div>{_paragraphs(section.summary)}</div></section>")
        blocks = "".join(self._block(block) for block in section.blocks)
        return (f"<section id=\"section-{number}\"><h2>{_e(section.title)}</h2>"
                f"<div class=\"section-summary\">{_paragraphs(section.summary)}</div>{blocks}</section>")

    def _block(self, block: ReportBlock) -> str:
        if isinstance(block, ParagraphBlock):
            return _paragraphs(block.text)
        if isinstance(block, BulletListBlock):
            return "<ul>" + "".join(f"<li>{_e(item)}</li>" for item in block.items) + "</ul>"
        if isinstance(block, TableBlock):
            head = "".join(f"<th scope=\"col\">{_e(column.label)}</th>" for column in block.columns)
            rows = "".join(
                "<tr>" + "".join(f"<td>{_e(row.get(column.key, '—'))}</td>" for column in block.columns) + "</tr>"
                for row in block.rows)
            caption = f"<caption>{_e(block.caption)}</caption>" if block.caption else ""
            wrap = "table-wrap table-short" if len(block.rows) <= _SHORT_TABLE_ROWS else "table-wrap"
            return f"<div class=\"{wrap}\"><table>{caption}<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>"
        if isinstance(block, CalloutBlock):
            return (f"<div class=\"callout {_CALLOUT[block.severity]}\" role=\"note\">"
                    f"<div class=\"callout-title\">{_e(block.title)}</div>{_paragraphs(block.text)}</div>")
        return ""  # unknown block types are never rendered

    def _attention_points(self, item: AttentionPointsItem12) -> str:
        if not item.points:
            return ("<section><h2>Puntos de atención</h2>"
                    "<p>No se identificaron puntos que requieran atención especial.</p></section>")
        points = []
        for point in item.points:
            css, label = _ATTENTION[point.severity]
            points.append(f"<div class=\"attention {css}\"><div class=\"badge\">Prioridad {label}</div>"
                          f"<h3>{_e(point.title)}</h3>{_paragraphs(point.text)}</div>")
        return f"<section><h2>Puntos de atención</h2>{''.join(points)}</section>"

    def _notes(self, item: NotesItem12) -> str:
        return f"<section class=\"notes\"><h2>Notas</h2>{_paragraphs(item.text)}</section>"

    def _methodology_notes(self, item: MethodologyNotesItem12) -> str:
        if not item.notes:
            return ""
        items = "".join(
            f"<li><strong>{_e(_NOTE_TITLES[note.kind])}:</strong> {_e(note.text)}</li>" for note in item.notes)
        return f"<section class=\"notes\"><h2>Notas y metodología</h2><ul>{items}</ul></section>"
