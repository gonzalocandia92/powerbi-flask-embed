"""Print layout policies for the WeasyPrint adapter (and nothing else).

The versioned HTML renderers are the canonical document and know nothing about PDF engines. WeasyPrint
implements less of CSS Grid/Flexbox than the browser the HTML was designed against, so
``WeasyPrintPdfRenderer`` lays a small stylesheet over the document. It lives here, not inside the
renderer, so that:

* each compensation is a named, documented ``PrintPolicy`` that can be tested or dropped on its own;
* the HTML renderers, ``ArtifactService`` and the HTTP layer never see it;
* another ``PdfRenderer`` implementation simply does not import this module.

Policies are overrides (``!important`` where the document's own print rules would win): ``@page``,
``break-*``, repeated table headers, colours and the theme variables all keep coming from the document.
Goal: stable A4 output, not a pixel-for-pixel copy of the browser layout.

Findings behind the policies (WeasyPrint 70): grid tracks sized with bare ``fr`` grow to the content's
min-content width and push columns outside the page; ``repeat(auto-fit, minmax(..))`` is not supported;
``flex-basis`` ignores padding and border (so bases here are ``width`` values); grids are not
fragmented across pages, so a two-column ``.cols-feature`` that spans a page break is clipped or leaves
the page nearly empty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


def _important(css: str) -> str:
    """Mark every declaration ``!important``: stylesheets handed to WeasyPrint do not outrank the
    document's own ``@media print`` rules at equal specificity, and these policies must always win."""
    return re.sub(r"(?<=[^\s;{}])\s*(?=[;}])(?<!important)", " !important", css.replace(" !important", ""))


@dataclass(frozen=True)
class PrintPolicy:
    name: str
    reason: str
    css: str


PRINT_POLICIES: tuple[PrintPolicy, ...] = (
    PrintPolicy(
        "shrinkable-grid-tracks",
        "Bare `fr` tracks cannot shrink below their content: the hero/split columns overflow the page.",
        """
.hero{grid-template-columns:minmax(0,1.5fr) minmax(0,.9fr) !important}
.cols-split{grid-template-columns:minmax(0,1fr) minmax(0,1fr) !important}
.bar-row{grid-template-columns:minmax(70px,1.1fr) minmax(0,2.4fr) 6.5rem !important}
.chart-ranking .bar-row{grid-template-columns:1.4rem minmax(70px,1.1fr) minmax(0,2.4fr) 5.5rem 4.5rem !important}
.hero>*,.cols>*,.rail>*{min-width:0}
""",
    ),
    PrintPolicy(
        "feature-columns-stack",
        "A two-column `.cols-feature` is not fragmented across pages by WeasyPrint (clipped rail, empty "
        "pages), so in print the main column is followed by the rail, full width. Theme `--feature-columns` "
        "is therefore not applied to PDFs. The rail is a plain block too (a grid would not fragment either), so a "
        "bordered rail (html-v4) can continue on the next page instead of being clipped.",
        """
.cols-feature{display:block !important}
.cols-feature>.rail{margin-top:18px;display:block !important}
.rail>*+*{margin-top:10px}
""",
    ),
    PrintPolicy(
        "wrapping-variable-count-grids",
        "`repeat(auto-fit, minmax(..))` is unsupported. Variable-count groups (V1/V2 `.kpis`, V3 `.strip` "
        "and `.cards`) become wrapping flex rows with the browser's minimum item width; `width` is the "
        "basis because WeasyPrint's `flex-basis` ignores padding.",
        """
.kpis,.strip,.cards{display:flex !important;flex-wrap:wrap;gap:10px}
.kpis>*,.cards>*,.strip>*{flex:1 1 auto;min-width:0}
.kpis>*{width:200px}
.cards>*{width:170px}
.strip>*{width:22%}
""",
    ),
    PrintPolicy(
        "explicit-attention-columns",
        "`.attn-grid` renders correctly with three explicit print columns (the document's own print rule); "
        "kept unchanged on purpose, only restated so an `auto-fit` base rule cannot win.",
        """
.attn-grid{grid-template-columns:repeat(3,minmax(0,1fr)) !important}
""",
    ),
    PrintPolicy(
        "tables-fit-the-page",
        "Table cells keep their min-content width, which widens narrow columns beyond the page: long "
        "words may wrap and padding is tighter.",
        """
th,td{overflow-wrap:anywhere}
""",
    ),
    PrintPolicy(
        "compact-type-scale",
        "A4 executive density: the browser design is sized for screens (rem on a 16px root, 10.5pt print "
        "body). Explicit point sizes keep a clear hierarchy (hero > section > KPI > body > table > caption) "
        "without a blind global shrink; tables and captions stay at or above 8.5pt for print legibility. "
        "KPI figures use `overflow-wrap:anywhere` because the fallback serif is wider than the intended "
        "display font.",
        _important("""
body{font-size:9.25pt;line-height:1.45}
.masthead h1{font-size:13pt}
.subtitle{font-size:9.25pt}
.period{font-size:8.5pt}
.hero-headline{font-size:23pt;line-height:1.12}
.hero-deck{font-size:10.5pt;margin-top:10px}
.chip{font-size:8pt;padding:2px 10px}
.hero-stat-label{font-size:9.25pt}
.hero-stat-text{font-size:8.5pt}
.metric-label,.card-label{font-size:7.5pt}
.metric-value{font-size:18pt !important;overflow-wrap:anywhere}
.card-value{font-size:17pt !important;overflow-wrap:anywhere}
.metric-secondary,.card-secondary,.card-text{font-size:8.5pt}
.trend{font-size:7.5pt}
.sec-number{font-size:11pt}
.sec h2,.plain h2{font-size:18pt;line-height:1.2}
.lede{font-size:10pt}
.pull p{font-size:12pt}
.pull-stat-value{font-size:18pt}
.pull-stat-label{font-size:8.5pt}
.annotation{font-size:8.5pt}
.callout{font-size:9pt}
.bar-label,.bar-value{font-size:9pt}
.bar-secondary,.rank{font-size:8.5pt}
table{font-size:8.75pt}
caption{font-size:8.5pt}
.attn h3{font-size:10.5pt}
.badge{font-size:7.5pt}
.notes{font-size:9pt}
.notes h2{font-size:12pt}
footer{font-size:8pt}
"""),
    ),
    PrintPolicy(
        "compact-spacing",
        "Tighter paddings, gaps and vertical margins than the screen design, so the report spends less "
        "height on whitespace without looking cramped (cards and sections keep a visible rhythm).",
        _important("""
.masthead{padding-bottom:12px;margin-bottom:16px}
.hero{padding:20px 22px;margin-bottom:14px}
.chips{margin-top:12px;gap:6px}
.hero-stat{padding:14px 12px}
.hero-highlights{margin-bottom:14px}
.hero-highlights li{margin-bottom:3px}
.strip{margin-bottom:16px}
.metric,.card{padding:10px 12px}
.sec{padding:14px 16px;margin-bottom:14px}
.sec-head{margin-bottom:8px}
.lede{margin-bottom:10px}
.stack>*+*{margin-top:12px}
.cols-feature>.rail{margin-top:12px}
.rail{gap:10px}
.cols{gap:14px}
.plain{margin-bottom:14px}
.plain h2{margin-bottom:8px}
.attn-grid{gap:10px}
.attn{padding:10px 12px}
.pull{padding:12px 16px}
.callout{padding:8px 12px}
.chart-title{margin-bottom:6px}
.bars{gap:6px}
ul.list li{margin-bottom:3px}
p+p{margin-top:8px}
th,td{padding:4px 6px !important}
footer{margin-top:20px;padding-top:10px}
"""),
    ),
    PrintPolicy(
        "hero-stat-value-fits",
        "The hero figure sits in the narrow right column and must never be clipped or wrapped, whatever its "
        "length. CSS cannot measure text, so the size is fixed for the longest realistic figure "
        "(`1.234.567.890`, 13 glyphs, about 5.8em in the serif fallback) with headroom: the grid track and "
        "the box can shrink (`min-width:0`), the figure is a single line (`nowrap`), and `clamp()` is "
        "deliberately not used (not reliable in WeasyPrint).",
        _important("""
.hero-stat{min-width:0}
.hero-stat-value{font-size:21pt !important;line-height:1.05;white-space:nowrap;overflow-wrap:normal;min-width:0}
"""),
    ),
)


def weasyprint_print_css(policies: tuple[PrintPolicy, ...] = PRINT_POLICIES) -> str:
    """The stylesheet applied on top of the document (pure function: same input, same output)."""
    return "".join(policy.css for policy in policies)
