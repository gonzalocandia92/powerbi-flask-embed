"""Deterministic HTML rendering of a ``FinalReportV13`` (renderer ``html-v3``, V1.5 data story).

Consumes only ``FinalReportV13`` plus an explicit ``ReportTheme``: no LLM, no ``ComposedReportInput``, no evidence
store, no DB, no environment. Same report + same theme => byte-identical HTML.

The renderer owns ALL markup, CSS and visuals:
* every model-authored string is HTML-escaped; CSS classes come from fixed semantic maps, never from report content;
* bars are plain CSS (a numeric ``width`` computed here from the materialized values); no SVG, no JS, no network;
* layouts (``standard`` / ``feature`` / ``split``) and severities are translated through the theme's tokens;
* evidence refs and ``source_section_keys`` are internal provenance and are never printed;
* restrictive CSP (``default-src 'none'``), print rules for PDF (``break-inside``, ``print-color-adjust``).

It dispatches on each item's / block's ``type`` and reads only MATERIALIZED fields, so it presents FinalReport 1.3 and
1.3.1 alike: evidence references (typed or not) are provenance it never looks at.

``html-v1`` and ``html-v2`` are frozen for historical reports and share nothing with this module.
"""
from __future__ import annotations

from html import escape

from .report_theme import ReportTheme, get_theme
from .versions import FINAL_REPORT_SCHEMA_1_3, FINAL_REPORT_SCHEMA_1_3_1, HTML_RENDERER_V3

_TREND_GLYPH = {"up": ("▲", "en alza"), "down": ("▼", "en baja"), "stable": ("▬", "estable"),
                "neutral": ("●", "sin tendencia")}
_IMPACT_CLASS = {"positive": "impact-positive", "negative": "impact-negative", "neutral": "impact-neutral"}
_SEVERITY = {"high": ("sev-high", "Alta"), "medium": ("sev-medium", "Media"), "low": ("sev-low", "Baja")}
_CALLOUT = {"info": "callout-info", "warning": "callout-warning", "critical": "callout-critical"}
_NOTE_TITLES = {"methodology": "Metodología", "data_quality": "Calidad de datos", "general": "Nota"}
_SHORT_TABLE_ROWS = 15

_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--font-body);font-size:var(--base-font-px);line-height:1.6}
.doc{max-width:var(--content-max-px);margin:0 auto;padding:40px 28px 56px}
h1,h2,h3,p,ul,ol,figure,blockquote{margin:0}
p+p{margin-top:12px}
.masthead{padding-bottom:20px;margin-bottom:28px;border-bottom:1px solid var(--border)}
.masthead h1{font-size:1.15rem;font-weight:700;letter-spacing:.02em;color:var(--primary)}
.subtitle{margin-top:4px;color:var(--muted)}
.period{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;font-size:.85rem;color:var(--muted)}
.hero{display:grid;grid-template-columns:1.6fr .8fr;gap:var(--gap-px);align-items:stretch;background:var(--hero-background);color:var(--hero-ink);border-radius:calc(var(--radius-px) + 6px);padding:40px;margin-bottom:var(--gap-px)}
.hero-headline{font-family:var(--font-display);font-size:var(--hero-font-px);line-height:1.12;font-weight:400;letter-spacing:-.01em}
.hero-deck{margin-top:16px;font-size:1.1rem;opacity:.9;max-width:62ch}
.chips{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding:0;margin-top:20px}
.chip{border:1px solid rgba(255,255,255,.35);border-radius:999px;padding:3px 12px;font-size:.8rem}
.hero-stat{align-self:center;border:1px solid rgba(255,255,255,.3);border-radius:var(--radius-px);padding:22px;background:rgba(255,255,255,.08)}
.hero-stat-value{font-family:var(--font-display);font-size:3rem;line-height:1;font-weight:400}
.hero-stat-label{margin-top:8px;font-weight:600}
.hero-stat-text{margin-top:6px;font-size:.85rem;opacity:.85}
.hero-highlights{margin:0 0 var(--gap-px);padding:0 4px 0 22px;color:var(--ink)}
.hero-highlights li{margin-bottom:6px}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:0 0 calc(var(--gap-px) + 8px)}
.metric,.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-px);padding:16px 18px}
.metric-label,.card-label{font-size:.76rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.metric-value,.card-value{font-family:var(--font-display);font-size:1.9rem;line-height:1.2;margin-top:4px}
.metric-secondary,.card-secondary,.card-text{margin-top:2px;font-size:.84rem;color:var(--muted)}
.trend{display:inline-block;margin-top:8px;padding:1px 9px;border-radius:999px;font-size:.76rem;font-weight:700}
.impact-positive{color:var(--success);background:var(--success-soft)}
.impact-negative{color:var(--danger);background:var(--danger-soft)}
.impact-neutral{color:var(--muted);background:var(--surface-alt)}
.sec{background:var(--surface);border:1px solid var(--border);border-radius:calc(var(--radius-px) + 4px);padding:32px 34px;margin-bottom:var(--gap-px)}
.sec-head{display:flex;align-items:baseline;gap:14px;margin-bottom:14px}
.sec-number{font-family:var(--font-display);font-size:1rem;color:var(--primary);font-weight:700}
.sec h2,.plain h2{font-family:var(--font-display);font-size:var(--heading-font-px);font-weight:400;line-height:1.2}
.lede{font-size:1.08rem;margin-bottom:18px;max-width:75ch}
.cols{display:grid;gap:var(--gap-px);align-items:start}
.cols-feature{grid-template-columns:var(--feature-columns)}
.cols-split{grid-template-columns:var(--split-columns)}
.stack>*+*{margin-top:18px}
.rail{display:grid;gap:14px;align-content:start}
.rail>*+*{margin-top:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.rail .cards{grid-template-columns:1fr}
.card{background:var(--surface-alt)}
.card-value{font-size:1.5rem}
ul.list{padding-left:22px}
ul.list li{margin-bottom:6px}
.pull{border-left:4px solid var(--primary);background:var(--primary-soft);border-radius:0 var(--radius-px) var(--radius-px) 0;padding:18px 22px}
.pull p{font-family:var(--font-display);font-size:1.3rem;line-height:1.4}
.pull-stat{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 12px;margin-top:10px}
.pull-stat-value{font-family:var(--font-display);font-size:1.9rem;color:var(--primary)}
.pull-stat-label{font-size:.85rem;color:var(--muted)}
.annotation{border-left:3px solid var(--border);padding:2px 0 2px 14px;font-size:.86rem;color:var(--muted)}
.callout{border-left:4px solid;border-radius:6px;padding:12px 16px}
.callout-title{font-weight:700;margin-bottom:2px}
.callout-info{border-color:var(--info);background:var(--info-soft)}
.callout-warning{border-color:var(--warning);background:var(--warning-soft)}
.callout-critical{border-color:var(--danger);background:var(--danger-soft)}
.chart{margin:0}
.chart-title{font-weight:700;margin-bottom:10px}
.bars{list-style:none;padding:0;display:grid;gap:10px}
.bar-row{display:grid;grid-template-columns:minmax(90px,1.1fr) 2.4fr 6.5rem;gap:6px 12px;align-items:center}
.chart-ranking .bar-row{grid-template-columns:1.4rem minmax(90px,1.1fr) 2.4fr 5.5rem 4.5rem}
.rank{font-size:.8rem;font-weight:700;color:var(--muted)}
.bar-label{font-size:.92rem;overflow-wrap:anywhere}
.bar-track{display:block;height:14px;background:var(--surface-alt);border:1px solid var(--border);border-radius:999px;overflow:hidden}
.bar-fill{display:block;height:100%;background:var(--primary);border-radius:999px}
.bar-fill.neg{background:var(--danger)}
.bar-value{font-weight:700;font-variant-numeric:tabular-nums;text-align:right}
.bar-secondary{font-size:.85rem;color:var(--muted);font-variant-numeric:tabular-nums;text-align:right}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.92rem}
caption{caption-side:top;text-align:left;font-size:.85rem;color:var(--muted);padding-bottom:6px}
th,td{padding:8px 12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
thead th{background:var(--surface-alt);border-bottom:2px solid var(--border)}
.unavailable{background:var(--info-soft);border-radius:var(--radius-px);padding:14px 18px}
.plain{margin-bottom:var(--gap-px)}
.plain h2{margin-bottom:12px}
.attn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.attn{background:var(--surface);border:1px solid var(--border);border-top:4px solid;border-radius:var(--radius-px);padding:16px 18px}
.attn h3{font-size:1.02rem;margin:4px 0 6px}
.badge{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.sev-high{border-top-color:var(--danger)}.sev-high .badge{color:var(--danger)}
.sev-medium{border-top-color:var(--warning)}.sev-medium .badge{color:var(--warning)}
.sev-low{border-top-color:var(--info)}.sev-low .badge{color:var(--info)}
.notes{font-size:.9rem;color:var(--muted)}
.notes h2{font-size:1.1rem;color:var(--ink)}
.notes ul{padding-left:22px}
footer{margin-top:36px;padding-top:14px;border-top:1px solid var(--border);font-size:.8rem;color:var(--muted)}
@media screen and (max-width:BREAKPOINTpx){
.doc{padding:20px 16px 36px}
.hero{grid-template-columns:1fr;padding:26px 22px}
.strip{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.metric-value{font-size:1.5rem}
.hero-headline{font-size:calc(var(--hero-font-px) * .68)}
.cols-feature,.cols-split{grid-template-columns:1fr}
.sec{padding:22px 18px}
.bar-row{grid-template-columns:1fr auto}
.bar-row .bar-track{grid-column:1 / -1;order:3}
.chart-ranking .bar-row{grid-template-columns:1.4rem 1fr auto}
.chart-ranking .bar-secondary{grid-column:2 / -1;text-align:left}
}
@page{size:A4;margin:14mm}
@media print{
html,body{background:#fff}
body{font-size:10.5pt;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.doc{max-width:none;padding:0}
.hero{grid-template-columns:1.6fr .8fr;padding:26px;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.hero-headline{font-size:28pt}
.cols-feature{grid-template-columns:var(--feature-columns)}
.cols-split{grid-template-columns:var(--split-columns)}
.sec{padding:18px 20px}
.strip{grid-template-columns:repeat(4,minmax(0,1fr))}
.attn-grid{grid-template-columns:repeat(3,minmax(0,1fr))}
.hero,.metric,.card,.attn,.callout,.pull,.chart,.annotation,.hero-stat,tr,.unavailable{break-inside:avoid}
.sec-head,h2,h3,caption,.chart-title{break-after:avoid}
.bar-fill,.badge,.trend,.pull,.callout,.attn{-webkit-print-color-adjust:exact;print-color-adjust:exact}
thead{display:table-header-group}
.table-wrap{overflow:visible}
footer{break-inside:avoid}}
"""


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _paragraphs(text: str) -> str:
    parts = [chunk.strip() for chunk in text.replace("\r\n", "\n").split("\n\n") if chunk.strip()]
    return "".join(f"<p>{_e(part).replace(chr(10), '<br>')}</p>" for part in parts)


def _width(value: float, scale: float) -> str:
    """Bar width in percent: a purely visual proportion of the (already validated) value; 1 decimal."""
    if scale <= 0:
        return "0"
    percent = max(0.0, min(100.0, abs(value) / scale * 100))
    if 0 < percent < 1:
        percent = 1.0
    return f"{percent:.1f}"


class HtmlReportRendererV3:
    """``FinalReportV13`` (+ theme) -> standalone HTML document (string). Deterministic and DB-free.

    Its identity is persisted next to every HTML it produces (``renderer_version``): a change that alters
    existing output means a NEW renderer version, never an edit of this one in place.
    """

    renderer_version = HTML_RENDERER_V3
    supported_schema_versions = (FINAL_REPORT_SCHEMA_1_3, FINAL_REPORT_SCHEMA_1_3_1)

    def __init__(self, theme: ReportTheme | None = None):
        self.theme = theme if theme is not None else get_theme()

    def render(self, report, theme: ReportTheme | None = None) -> str:
        theme = theme if theme is not None else self.theme
        numbers = iter(range(1, len(report.items) + 1))
        body = [self._masthead(report)]
        for item in report.items:  # the JSON order is the authority; nothing is hoisted, filtered or regrouped
            body.append(self._item(item, numbers))
        body.append("<footer>Informe generado automáticamente por KLARA a partir de datos analíticos verificados.</footer>")
        css = theme.css_variables() + _CSS.replace("BREAKPOINT", str(theme.breakpoint_px))
        return (
            "<!DOCTYPE html>\n<html lang=\"es\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
            f"<title>{_e(report.title)}</title><style>{css}</style></head>"
            f"<body><main class=\"doc\">{''.join(body)}</main></body></html>\n"
        )

    # -- dispatch ---------------------------------------------------------------------------
    def _item(self, item, numbers) -> str:
        kind = getattr(item, "type", None)
        if kind == "hero":
            return self._hero(item)
        if kind == "metric_strip":
            return self._metric_strip(item)
        if kind == "section":
            return self._section(item, next(numbers))
        if kind == "attention_grid":
            return self._attention_grid(item)
        if kind == "notes":
            return f"<section class=\"plain notes\"><h2>Notas</h2>{_paragraphs(item.text)}</section>"
        if kind == "methodology_notes":
            return self._methodology(item)
        return ""  # unknown item types are never rendered

    # -- parts ------------------------------------------------------------------------------
    def _masthead(self, report) -> str:
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
        return f"<header class=\"masthead\"><h1>{_e(report.title)}</h1>{subtitle}{period_html}</header>"

    def _hero(self, hero) -> str:
        deck = f"<p class=\"hero-deck\">{_e(hero.deck)}</p>" if hero.deck else ""
        chips = "".join(f"<li class=\"chip\">{_e(text)}</li>" for text in hero.metadata)
        chips = f"<ul class=\"chips\">{chips}</ul>" if chips else ""
        stat = ""
        if hero.highlight is not None:
            h = hero.highlight
            text = f"<div class=\"hero-stat-text\">{_e(h.supporting_text)}</div>" if h.supporting_text else ""
            stat = (f"<aside class=\"hero-stat\"><div class=\"hero-stat-value\">{_e(h.value)}</div>"
                    f"<div class=\"hero-stat-label\">{_e(h.label)}</div>{text}</aside>")
        bullets = "".join(f"<li>{_e(entry.text)}</li>" for entry in hero.highlights)
        bullets = f"<ul class=\"hero-highlights\">{bullets}</ul>" if bullets else ""
        return (f"<section class=\"hero\"><div><h2 class=\"hero-headline\">{_e(hero.headline)}</h2>{deck}{chips}</div>"
                f"{stat}</section>{bullets}")

    @staticmethod
    def _trend(card) -> str:
        if not card.trend:
            return ""
        glyph, label = _TREND_GLYPH[card.trend]
        css = _IMPACT_CLASS[card.impact or "neutral"]
        return f"<span class=\"trend {css}\"><span aria-hidden=\"true\">{glyph}</span> {_e(label)}</span>"

    def _card(self, card, kind: str) -> str:
        secondary = f"<div class=\"{kind}-secondary\">{_e(card.secondary_value)}</div>" if card.secondary_value else ""
        text = f"<div class=\"card-text\">{_e(card.supporting_text)}</div>" if card.supporting_text else ""
        return (f"<div class=\"{kind}\"><div class=\"{kind}-label\">{_e(card.label)}</div>"
                f"<div class=\"{kind}-value\">{_e(card.value)}</div>{secondary}{text}{self._trend(card)}</div>")

    def _metric_strip(self, strip) -> str:
        if not strip.metrics:
            return ""
        cards = "".join(self._card(metric, "metric") for metric in strip.metrics)
        return f"<section class=\"strip\" aria-label=\"Indicadores destacados\">{cards}</section>"

    def _section(self, section, number: int) -> str:
        head = (f"<div class=\"sec-head\"><span class=\"sec-number\">{number:02d}</span>"
                f"<h2>{_e(section.title)}</h2></div>")
        if section.status == "unavailable":
            return (f"<section class=\"sec\" id=\"section-{number}\">{head}<div class=\"unavailable\" role=\"note\">"
                    f"{_paragraphs(section.summary)}</div></section>")
        lede = f"<div class=\"lede\">{_paragraphs(section.summary)}</div>"
        main = "".join(self._block(block) for block in section.blocks)
        rail = "".join(self._block(block) for block in section.secondary_blocks)
        if section.layout == "feature" and rail:
            content = f"<div class=\"cols cols-feature\"><div class=\"stack\">{main}</div><aside class=\"rail\">{rail}</aside></div>"
        elif section.layout == "split" and rail:
            content = f"<div class=\"cols cols-split\"><div class=\"stack\">{main}</div><div class=\"stack\">{rail}</div></div>"
        else:  # standard (or a feature/split without a second column): one column
            content = f"<div class=\"stack\">{main}{rail}</div>"
        return f"<section class=\"sec\" id=\"section-{number}\">{head}{lede}{content}</section>"

    def _block(self, block) -> str:
        kind = getattr(block, "type", None)
        if kind == "paragraph":
            return _paragraphs(block.text)
        if kind == "bullet_list":
            return "<ul class=\"list\">" + "".join(f"<li>{_e(item)}</li>" for item in block.items) + "</ul>"
        if kind == "table":
            return self._table(block)
        if kind == "callout":
            return (f"<div class=\"callout {_CALLOUT[block.severity]}\" role=\"note\">"
                    f"<div class=\"callout-title\">{_e(block.title)}</div>{_paragraphs(block.text)}</div>")
        if kind == "pull_quote":
            return self._pull_quote(block)
        if kind == "annotation":
            return f"<p class=\"annotation\">{_e(block.text)}</p>"
        if kind == "metric_cards":
            return "<div class=\"cards\">" + "".join(self._card(card, "card") for card in block.cards) + "</div>"
        if kind == "bar_chart":
            return self._bar_chart(block)
        return ""  # unknown block types are never rendered

    @staticmethod
    def _table(block) -> str:
        head = "".join(f"<th scope=\"col\">{_e(column.label)}</th>" for column in block.columns)
        rows = "".join(
            "<tr>" + "".join(f"<td>{_e(row.get(column.key, '—'))}</td>" for column in block.columns) + "</tr>"
            for row in block.rows)
        caption = f"<caption>{_e(block.caption)}</caption>" if block.caption else ""
        return (f"<div class=\"table-wrap\"><table>{caption}<thead><tr>{head}</tr></thead>"
                f"<tbody>{rows}</tbody></table></div>")

    @staticmethod
    def _pull_quote(block) -> str:
        stat = ""
        if block.stat is not None:
            stat = (f"<div class=\"pull-stat\"><span class=\"pull-stat-value\">{_e(block.stat.value)}</span>"
                    f"<span class=\"pull-stat-label\">{_e(block.stat.label)}</span></div>")
        return f"<blockquote class=\"pull\"><p>{_e(block.text)}</p>{stat}</blockquote>"

    @staticmethod
    def _bar_chart(chart) -> str:
        if chart.variant == "distribution":
            scale = sum(max(item.value, 0) for item in chart.items)
        else:
            scale = max((abs(item.value) for item in chart.items), default=0)
        ranking = chart.variant == "ranking"
        rows = []
        for position, item in enumerate(chart.items, start=1):
            fill = "bar-fill neg" if item.value < 0 else "bar-fill"
            rank = f"<span class=\"rank\">{position}</span>" if ranking else ""
            secondary = (f"<span class=\"bar-secondary\">{_e(item.secondary_formatted_value)}</span>"
                         if ranking and item.secondary_formatted_value else "")
            if ranking and not secondary and chart.secondary_series_ref is not None:
                secondary = "<span class=\"bar-secondary\">—</span>"
            rows.append(
                f"<li class=\"bar-row\">{rank}<span class=\"bar-label\">{_e(item.label)}</span>"
                f"<span class=\"bar-track\"><span class=\"{fill}\" style=\"width:{_width(item.value, scale)}%\"></span></span>"
                f"<span class=\"bar-value\">{_e(item.formatted_value)}</span>{secondary}</li>")
        return (f"<figure class=\"chart chart-{chart.variant}\"><figcaption class=\"chart-title\">{_e(chart.title)}</figcaption>"
                f"<ol class=\"bars\">{''.join(rows)}</ol></figure>")

    def _attention_grid(self, grid) -> str:
        if not grid.points:
            return ("<section class=\"plain\"><h2>Puntos de atención</h2>"
                    "<p>No se identificaron puntos que requieran atención especial.</p></section>")
        cards = []
        for point in grid.points:
            css, label = _SEVERITY[point.severity]
            cards.append(f"<div class=\"attn {css}\"><div class=\"badge\">Prioridad {label}</div>"
                         f"<h3>{_e(point.title)}</h3>{_paragraphs(point.text)}</div>")
        return f"<section class=\"plain\"><h2>Puntos de atención</h2><div class=\"attn-grid\">{''.join(cards)}</div></section>"

    @staticmethod
    def _methodology(item) -> str:
        if not item.notes:
            return ""
        entries = "".join(f"<li><strong>{_e(_NOTE_TITLES[note.kind])}:</strong> {_e(note.text)}</li>"
                          for note in item.notes)
        return f"<section class=\"plain notes\"><h2>Notas y metodología</h2><ul>{entries}</ul></section>"
