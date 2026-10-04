"""Single home of the version identifiers used by reporting artifacts.

Nothing here imports a model, a renderer or SQLAlchemy: it is the vocabulary the
registries, the artifact store and the persisted rows share. Add a constant when a
new schema/renderer/artifact kind is introduced; never repeat the literals elsewhere.

``CURRENT_*`` is what NEW runs produce. A persisted artifact always carries its own
version, and it is resolved through the registries, never through ``CURRENT_*``.
"""
from __future__ import annotations

# FinalReport schema versions (``FinalReport.schema_version``).
FINAL_REPORT_SCHEMA_1_1 = "1.1"
FINAL_REPORT_SCHEMA_1_2 = "1.2"
FINAL_REPORT_SCHEMA_1_3 = "1.3"

# HTML renderer identities (``report_run_artifacts.renderer_version``).
HTML_RENDERER_V1 = "html-v1"
HTML_RENDERER_V2 = "html-v2"
HTML_RENDERER_V3 = "html-v3"

# New persisted runs (ReportRun) produce 1.3 / html-v3 (V1.5). 1.1 / html-v1 and 1.2 / html-v2 stay readable and
# renderable forever. The legacy synchronous ``/generate`` path keeps 1.1 / html-v1 on purpose (debug/comparison
# path); it does not read these constants.
CURRENT_FINAL_REPORT_SCHEMA = FINAL_REPORT_SCHEMA_1_3
CURRENT_HTML_RENDERER = HTML_RENDERER_V3

# ``report_run_artifacts.artifact_type``. Only the first two are persisted today;
# the rest are reserved names so later stages do not invent their own spellings.
ARTIFACT_FINAL_REPORT = "final_report"
ARTIFACT_HTML = "html"
ARTIFACT_PDF = "pdf"
ARTIFACT_MARKDOWN = "markdown"
ARTIFACT_REPORT_DRAFT = "report_draft"

CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_HTML = "text/html; charset=utf-8"
