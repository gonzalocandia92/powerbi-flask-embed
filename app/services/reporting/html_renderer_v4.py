"""``html-v4``: editorial composition of sections for ``FinalReport`` 1.3.1.

Same contract and the same deterministic, DB-free, script-free, CSP-compatible document as ``html-v3``; what changes
is how a section's layout is presented:

* every section carries a semantic class (``sec-standard`` / ``sec-feature`` / ``sec-split``) taken ONLY from
  ``section.layout``; nothing here looks at the text to decide anything;
* in ``feature`` and ``split`` the lede is a full-width header, separated from the columns;
* ``feature`` columns come from the theme (``aklara-editorial-v2``: ~60/40) instead of a value hard-coded here;
* the rail (``secondary_blocks``) is a quiet container (alt surface + border), so complementary content reads as such.

``html-v3`` is a persisted identity and is NOT touched: this renderer reuses its (frozen) block rendering and adds a
CSS layer on top. Anything that changes html-v3's output belongs in a new version, not in an edit of V3.
"""
from __future__ import annotations

from .html_renderer_v3 import HtmlReportRendererV3, _CSS as _CSS_V3, _e, _paragraphs
from .report_theme import AKLARA_EDITORIAL_V2, ReportTheme, get_theme
from .versions import FINAL_REPORT_SCHEMA_1_3_1, HTML_RENDERER_V4

_LAYOUT_CLASS = {"standard": "sec-standard", "feature": "sec-feature", "split": "sec-split"}

# Layered AFTER html-v3's stylesheet (same tokens, no literal colors). Print rules come last so they win over the screen ones.
_CSS_V4 = """
.sec-feature .lede,.sec-split .lede{max-width:none;padding-bottom:16px;margin-bottom:22px;border-bottom:1px solid var(--border)}
.rail{gap:16px;background:var(--surface-alt);border:1px solid var(--border);border-radius:var(--radius-px);padding:18px 20px}
@media screen and (max-width:BREAKPOINTpx){
.rail{padding:14px 16px}
.sec-feature .lede,.sec-split .lede{padding-bottom:12px;margin-bottom:16px}
}
@media print{
.sec-feature .lede,.sec-split .lede{padding-bottom:10px;margin-bottom:14px}
.rail{padding:12px 14px}
.rail>*{break-inside:avoid}}
"""


class HtmlReportRendererV4(HtmlReportRendererV3):
    """``FinalReportV131`` (+ theme) -> standalone HTML document (string). Deterministic and DB-free."""

    renderer_version = HTML_RENDERER_V4
    supported_schema_versions = (FINAL_REPORT_SCHEMA_1_3_1,)

    def __init__(self, theme: ReportTheme | None = None):
        super().__init__(theme if theme is not None else get_theme(AKLARA_EDITORIAL_V2.theme_id))

    def render(self, report, theme: ReportTheme | None = None) -> str:
        theme = theme if theme is not None else self.theme
        numbers = iter(range(1, len(report.items) + 1))
        body = [self._masthead(report)]
        for item in report.items:  # the JSON order is the authority; nothing is hoisted, filtered or regrouped
            body.append(self._item(item, numbers))
        body.append("<footer>Informe generado automáticamente por KLARA a partir de datos analíticos verificados.</footer>")
        css = theme.css_variables() + (_CSS_V3 + _CSS_V4).replace("BREAKPOINT", str(theme.breakpoint_px))
        return (
            "<!DOCTYPE html>\n<html lang=\"es\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
            f"<title>{_e(report.title)}</title><style>{css}</style></head>"
            f"<body><main class=\"doc\">{''.join(body)}</main></body></html>\n"
        )

    def _section(self, section, number: int) -> str:
        layout = _LAYOUT_CLASS.get(section.layout, "sec-standard")
        head = (f"<div class=\"sec-head\"><span class=\"sec-number\">{number:02d}</span>"
                f"<h2>{_e(section.title)}</h2></div>")
        if section.status == "unavailable":
            return (f"<section class=\"sec {layout}\" id=\"section-{number}\">{head}<div class=\"unavailable\" role=\"note\">"
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
        return f"<section class=\"sec {layout}\" id=\"section-{number}\">{head}{lede}{content}</section>"
