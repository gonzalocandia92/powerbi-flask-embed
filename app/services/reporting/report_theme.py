"""Report themes: the ONLY place colors, typography and spacing live (V1.5).

A ``ReportTheme`` is internal, code-defined configuration. It is selected by ID and never built from LLM output, a
``structure_prompt`` or an API payload: there is no "custom CSS" path anywhere. A ``FinalReport`` carries semantics
(``severity=high``, ``layout=feature``); the renderer maps them to the tokens below. Every token is validated
against a strict pattern on construction, so even a mistake in a future theme cannot inject markup or CSS rules into
the document.

Adding a theme (e.g. per client) = one ``ReportTheme(...)`` and one ``register_theme`` call; no report, writer or
renderer logic changes. A theme is immutable and passed explicitly (``renderer.render(report, theme)``): the renderer
reads no environment variables and no global mutable state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_FONT = re.compile(r"^[A-Za-z0-9 ,'\"_-]{1,200}$")
_GRID = re.compile(r"^\d(\.\d{1,2})?fr \d(\.\d{1,2})?fr$")
_THEME_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")

_COLOR_FIELDS = (
    "ink", "muted", "paper", "surface", "surface_alt", "primary", "primary_soft", "accent", "danger", "danger_soft",
    "warning", "warning_soft", "success", "success_soft", "info", "info_soft", "border", "hero_ink", "hero_background",
)


@dataclass(frozen=True)
class ReportTheme:
    theme_id: str
    # colors
    ink: str
    muted: str
    paper: str            # page background
    surface: str          # cards / document sheet
    surface_alt: str
    primary: str
    primary_soft: str
    accent: str
    danger: str
    danger_soft: str
    warning: str
    warning_soft: str
    success: str
    success_soft: str
    info: str
    info_soft: str
    border: str
    hero_ink: str
    hero_background: str
    # typography
    font_body: str
    font_display: str
    base_font_px: int = 16
    hero_font_px: int = 44
    heading_font_px: int = 26
    # geometry the renderer maps semantic layouts onto
    content_max_px: int = 1080
    feature_columns: str = "1.45fr 0.75fr"
    split_columns: str = "1fr 1fr"
    gap_px: int = 28
    radius_px: int = 12
    breakpoint_px: int = 860

    def __post_init__(self) -> None:
        if not _THEME_ID.match(self.theme_id):
            raise ValueError("invalid theme_id")
        for name in _COLOR_FIELDS:
            if not _COLOR.match(getattr(self, name)):
                raise ValueError(f"theme color {name!r} must be #rrggbb")
        for name in ("font_body", "font_display"):
            if not _FONT.match(getattr(self, name)):
                raise ValueError(f"theme font {name!r} has unsupported characters")
        for name in ("feature_columns", "split_columns"):
            if not _GRID.match(getattr(self, name)):
                raise ValueError(f"theme {name!r} must look like '1.45fr 0.75fr'")
        bounds = {"base_font_px": (12, 22), "hero_font_px": (24, 72), "heading_font_px": (16, 40),
                  "content_max_px": (640, 1440), "gap_px": (8, 64), "radius_px": (0, 32), "breakpoint_px": (480, 1200)}
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"theme {name!r} must be an integer in [{low}, {high}]")

    def css_variables(self) -> str:
        """``:root{...}`` declarations; deterministic order (field order)."""
        names = [name for name in (f.name for f in fields(self)) if name != "theme_id"]
        parts = []
        for name in names:
            value = getattr(self, name)
            css_name = "--" + name.replace("_", "-")
            parts.append(f"{css_name}:{value}px" if isinstance(value, int) else f"{css_name}:{value}")
        return ":root{" + ";".join(parts) + "}"


AKLARA_EDITORIAL = ReportTheme(
    theme_id="aklara-editorial",
    ink="#1b1a2e", muted="#5f6073", paper="#f4f2ee", surface="#ffffff", surface_alt="#faf9f6",
    primary="#6256d9", primary_soft="#eeecfb", accent="#0f8b8d",
    danger="#b3261e", danger_soft="#fdecea", warning="#8a5a00", warning_soft="#fff4d6",
    success="#1c7c54", success_soft="#e4f4ec", info="#2f5d9e", info_soft="#e8f0fa",
    border="#e3e0d8", hero_ink="#ffffff", hero_background="#2a2559",
    font_body="Inter, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    font_display="Georgia, 'Times New Roman', serif",
)

# html-v4 theme: same palette and typography; the feature layout gives the complementary column ~40% of the width.
# ``aklara-editorial`` stays untouched for the historical html-v3.
AKLARA_EDITORIAL_V2 = replace(AKLARA_EDITORIAL, theme_id="aklara-editorial-v2", feature_columns="1.30fr 0.90fr")

_THEMES: dict[str, ReportTheme] = {AKLARA_EDITORIAL.theme_id: AKLARA_EDITORIAL,
                                   AKLARA_EDITORIAL_V2.theme_id: AKLARA_EDITORIAL_V2}
DEFAULT_THEME_ID = AKLARA_EDITORIAL.theme_id


def register_theme(theme: ReportTheme) -> None:
    if theme.theme_id in _THEMES:
        raise ValueError(f"Theme already registered: {theme.theme_id}")
    _THEMES[theme.theme_id] = theme


def get_theme(theme_id: str | None = None) -> ReportTheme:
    try:
        return _THEMES[theme_id or DEFAULT_THEME_ID]
    except KeyError:
        raise ValueError(f"Unknown report theme: {theme_id!r}") from None
