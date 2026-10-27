"""``ReportStructureSpec``: the user's structural intent for a report, as a strict, versioned contract.

It answers ONE question: *how does the user want the existing evidence organised?*
It never contains HTML, Markdown, CSS, colours, charts, DAX, answers or new questions.
Items reference questions by their stable ``key`` only. Pure Pydantic: no SQLAlchemy, no Flask,
no LLM, so it validates (and is tested) without a database.

Versioning mirrors ``FinalReport``: a persisted spec carries its ``schema_version`` and is
resolved through ``StructureSchemaRegistry``; nobody imports "the" current class to read
history. Adding ``2.0`` = freeze nothing, register another model next to ``ReportStructureSpecV10``.

V1.0 is deliberately shallow and non-recursive (no containers, no per-section ``components``:
they only pay off once a composer consumes them).

V1.1 (V1.3.1) is the MINIMAL evolution needed to express today's standard report as an
authoritative structure, which 1.0 cannot:

* ``kpi_grid.source_question_keys`` is an ELIGIBLE POOL (up to 50, i.e. every question), not "the
  first N questions": the writer picks which become KPI cards, exactly like the legacy writer did;
* ``methodology_notes``: client-readable notes derived from the semantic notes (and unavailable
  analyses) of the report's evidence, which the legacy writer produced implicitly.

1.0 specs (everything the planner interprets) stay valid and untouched; only the DEFAULT structure is 1.1.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

STRUCTURE_SCHEMA_1_0 = "1.0"
STRUCTURE_SCHEMA_1_1 = "1.1"
# What the StructurePlanner is asked to produce (and what ``structure_input_hash`` covers). Interpreted
# prompts stay 1.0: nothing the user can express needs 1.1.
CURRENT_STRUCTURE_SCHEMA = STRUCTURE_SCHEMA_1_0
# The deterministic default structure (no prompt / not compiled / fallback) is built as 1.1.
DEFAULT_STRUCTURE_SCHEMA = STRUCTURE_SCHEMA_1_1

MAX_STRUCTURE_ITEMS = 30
MAX_KPI_QUESTIONS = 8  # same ceiling as FinalReport.MAX_KPIS
MAX_KPI_POOL = 50  # 1.1: eligible sources for the grid (>= the 20 questions a definition can have)

QuestionKey = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
SectionKey = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$")]
Title = Annotated[str, Field(min_length=1, max_length=200)]
SourceQuestionKeys = Annotated[list[QuestionKey], Field(min_length=1, max_length=50)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _no_duplicates(keys: list[str], where: str) -> list[str]:
    if len(set(keys)) != len(keys):
        raise ValueError(f"{where}: source_question_keys cannot repeat a question")
    return keys


class _StructureSpecBase(_Strict):
    """Version-independent behaviour of a spec (the subclasses declare ``schema_version`` and ``items``)."""

    @model_validator(mode="after")
    def _coherent(self):
        for kind in SINGLETON_TYPES:
            if sum(1 for item in self.items if item.type == kind) > 1:
                raise ValueError(f"at most one {kind} item is allowed")
        keys = [item.key for item in self.items if isinstance(item, SectionItem)]
        if len(set(keys)) != len(keys):
            raise ValueError("section keys must be unique within the spec")
        return self

    def referenced_question_keys(self) -> set[str]:
        keys: set[str] = set()
        for item in self.items:
            keys.update(getattr(item, "source_question_keys", ()))
        return keys

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ExecutiveSummaryItem(_Strict):
    type: Literal["executive_summary"] = "executive_summary"


class KpiGridItem(_Strict):
    type: Literal["kpi_grid"] = "kpi_grid"
    source_question_keys: Annotated[list[QuestionKey], Field(min_length=1, max_length=MAX_KPI_QUESTIONS)]

    @model_validator(mode="after")
    def _unique(self) -> "KpiGridItem":
        _no_duplicates(self.source_question_keys, "kpi_grid")
        return self


class SectionItem(_Strict):
    type: Literal["section"] = "section"
    key: SectionKey
    title: Title
    source_question_keys: SourceQuestionKeys

    @model_validator(mode="after")
    def _unique(self) -> "SectionItem":
        _no_duplicates(self.source_question_keys, f"section {self.key!r}")
        return self


class AttentionPointsItem(_Strict):
    type: Literal["attention_points"] = "attention_points"


class NotesItem(_Strict):
    """Literal text the user asked to include (e.g. a methodological remark). Never model-invented."""
    type: Literal["notes"] = "notes"
    text: Annotated[str, Field(min_length=1, max_length=1000)]


StructureItem = Annotated[
    Union[ExecutiveSummaryItem, KpiGridItem, SectionItem, AttentionPointsItem, NotesItem],
    Field(discriminator="type"),
]

# Item types that make sense at most once per report (FinalReport has a single one of each).
SINGLETON_TYPES = ("executive_summary", "kpi_grid", "attention_points", "methodology_notes")


class MethodologyNotesItem(_Strict):
    """1.1: client-readable methodology/data-quality notes derived from the report's evidence."""
    type: Literal["methodology_notes"] = "methodology_notes"


class KpiGridItemV11(_Strict):
    """1.1: ``source_question_keys`` is the pool of questions KPI cards may be drawn from."""
    type: Literal["kpi_grid"] = "kpi_grid"
    source_question_keys: Annotated[list[QuestionKey], Field(min_length=1, max_length=MAX_KPI_POOL)]

    @model_validator(mode="after")
    def _unique(self) -> "KpiGridItemV11":
        _no_duplicates(self.source_question_keys, "kpi_grid")
        return self


StructureItemV11 = Annotated[
    Union[ExecutiveSummaryItem, KpiGridItemV11, SectionItem, AttentionPointsItem, NotesItem, MethodologyNotesItem],
    Field(discriminator="type"),
]


class ReportStructureSpecV10(_StructureSpecBase):
    schema_version: Literal["1.0"] = STRUCTURE_SCHEMA_1_0
    # Order of ``items`` IS the order of the report: nothing else defines it.
    items: Annotated[list[StructureItem], Field(min_length=1, max_length=MAX_STRUCTURE_ITEMS)]


class ReportStructureSpecV11(_StructureSpecBase):
    schema_version: Literal["1.1"] = STRUCTURE_SCHEMA_1_1
    items: Annotated[list[StructureItemV11], Field(min_length=1, max_length=MAX_STRUCTURE_ITEMS)]


# Any registered spec version (type hint for code that consumes a spec without caring which).
ReportStructureSpec = Union[ReportStructureSpecV10, ReportStructureSpecV11]


class StructureSchemaRegistry:
    """``schema_version -> Pydantic model`` for historical and current structure specs."""

    def __init__(self) -> None:
        self._models: dict[str, type[BaseModel]] = {}

    def register(self, schema_version: str, model: type[BaseModel]) -> None:
        if schema_version in self._models:
            raise ValueError(f"Structure schema already registered: {schema_version}")
        self._models[schema_version] = model

    def versions(self) -> tuple[str, ...]:
        return tuple(self._models)

    def get(self, schema_version: str | None) -> type[BaseModel]:
        try:
            return self._models[schema_version]  # type: ignore[index]
        except (KeyError, TypeError):
            raise UnknownStructureSchemaError(f"Unknown structure schema version: {schema_version!r}") from None

    def validate(self, raw: Mapping[str, Any], schema_version: str | None = None) -> BaseModel:
        """Validate ``raw`` with the model of ITS version (``schema_version`` wins when given and must agree)."""
        declared = raw.get("schema_version") if isinstance(raw, Mapping) else None
        if schema_version is not None and declared is not None and declared != schema_version:
            raise UnknownStructureSchemaError(
                f"Structure schema {schema_version!r} does not match its content ({declared!r})")
        return self.get(schema_version if schema_version is not None else declared).model_validate(raw)


class UnknownStructureSchemaError(ValueError):
    """A persisted/received structure schema version cannot be resolved."""


def build_default_structure_schemas() -> StructureSchemaRegistry:
    registry = StructureSchemaRegistry()
    registry.register(STRUCTURE_SCHEMA_1_0, ReportStructureSpecV10)
    registry.register(STRUCTURE_SCHEMA_1_1, ReportStructureSpecV11)
    return registry


structure_schemas = build_default_structure_schemas()
