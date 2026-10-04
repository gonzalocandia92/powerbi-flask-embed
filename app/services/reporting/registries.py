"""Version resolution for ``FinalReport`` schemas and HTML renderers.

This is the ONLY place that maps a persisted version string to an implementation.
Routes, the worker and the PDF export ask a registry; none of them compares
versions itself. Registries are small, DB-free and instantiable in tests.

    schema   = final_report_schemas.get("1.1")            # FinalReport 1.1 model
    report   = final_report_schemas.validate(raw_json)    # picks the model from raw["schema_version"]
    renderer = html_renderers.get_for_schema("html-v1", "1.1")
    html     = renderer.render(report)

Schema and renderer are related explicitly: every renderer declares which schema
versions it can present, and a renderer is never assumed from a schema (or vice
versa). Registering a new schema/renderer is one ``register`` call at the bottom.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from pydantic import BaseModel

from .final_report import FinalReport
from .final_report_v12 import FinalReportV12
from .final_report_v13 import FinalReportV13
from .html_renderer import HtmlReportRenderer
from .html_renderer_v2 import HtmlReportRendererV2
from .html_renderer_v3 import HtmlReportRendererV3
from .versions import (
    FINAL_REPORT_SCHEMA_1_1, FINAL_REPORT_SCHEMA_1_2, FINAL_REPORT_SCHEMA_1_3, HTML_RENDERER_V1, HTML_RENDERER_V2,
    HTML_RENDERER_V3,
)


class VersionResolutionError(ValueError):
    """Base class: a persisted/received version cannot be resolved."""


class UnknownSchemaVersionError(VersionResolutionError):
    pass


class UnknownRendererVersionError(VersionResolutionError):
    pass


class IncompatibleRendererError(VersionResolutionError):
    """The renderer exists but does not present this schema version."""


class SchemaMismatchError(VersionResolutionError):
    """The schema declared by the artifact and by its own JSON disagree."""


class HtmlRenderer(Protocol):
    def render(self, report: Any) -> str: ...


class FinalReportSchemaRegistry:
    """``schema_version -> Pydantic model`` for historical and current FinalReports."""

    def __init__(self) -> None:
        self._models: dict[str, type[BaseModel]] = {}

    def register(self, schema_version: str, model: type[BaseModel]) -> None:
        if schema_version in self._models:
            raise ValueError(f"FinalReport schema already registered: {schema_version}")
        self._models[schema_version] = model

    def versions(self) -> tuple[str, ...]:
        return tuple(self._models)

    def get(self, schema_version: str | None) -> type[BaseModel]:
        try:
            return self._models[schema_version]  # type: ignore[index]
        except (KeyError, TypeError):
            raise UnknownSchemaVersionError(f"Unknown FinalReport schema version: {schema_version!r}") from None

    def validate(self, raw: Mapping[str, Any], schema_version: str | None = None) -> BaseModel:
        """Validate ``raw`` with the model of ITS version.

        ``schema_version`` (e.g. the artifact column) is authoritative when given and
        must match the JSON's own ``schema_version``. Raises ``VersionResolutionError``
        for unknown/mismatched versions and ``pydantic.ValidationError`` for bad content.
        """
        declared = raw.get("schema_version") if isinstance(raw, Mapping) else None
        if schema_version is not None and declared is not None and declared != schema_version:
            raise SchemaMismatchError(
                f"Artifact schema {schema_version!r} does not match its content ({declared!r})")
        return self.get(schema_version if schema_version is not None else declared).model_validate(raw)


@dataclass(frozen=True)
class _RendererSpec:
    factory: Callable[[], HtmlRenderer]
    schema_versions: tuple[str, ...]


@dataclass
class HtmlRendererRegistry:
    """``renderer_version -> renderer`` plus the explicit schema/renderer association."""

    _specs: dict[str, _RendererSpec] = field(default_factory=dict)
    _defaults: dict[str, str] = field(default_factory=dict)
    _originals: dict[str, str] = field(default_factory=dict)

    def register(self, renderer_version: str, factory: Callable[[], HtmlRenderer], *,
                 schema_versions: tuple[str, ...], default_for: tuple[str, ...] = (),
                 original_for: tuple[str, ...] = ()) -> None:
        """Two distinct associations per schema version:

        ``original_for``: the schema's HISTORICAL renderer, used to present data that has no
        stored HTML (legacy runs). Claimed once and immutable: a newer renderer never takes it over.
        ``default_for``: the RECOMMENDED renderer for NEW output; free to move to a newer renderer.
        """
        if renderer_version in self._specs:
            raise ValueError(f"HTML renderer already registered: {renderer_version}")
        if not (set(default_for) | set(original_for)) <= set(schema_versions):
            raise ValueError("A renderer can only be original/default for schemas it supports")
        if claimed := set(original_for) & set(self._originals):
            raise ValueError(f"Original renderer already fixed for schema(s): {sorted(claimed)}")
        self._specs[renderer_version] = _RendererSpec(factory, tuple(schema_versions))
        self._defaults.update({schema: renderer_version for schema in default_for})
        self._originals.update({schema: renderer_version for schema in original_for})

    def versions(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def supports(self, renderer_version: str, schema_version: str) -> bool:
        spec = self._specs.get(renderer_version)
        return spec is not None and schema_version in spec.schema_versions

    def get(self, renderer_version: str | None) -> HtmlRenderer:
        spec = self._specs.get(renderer_version)  # type: ignore[arg-type]
        if spec is None:
            raise UnknownRendererVersionError(f"Unknown HTML renderer version: {renderer_version!r}")
        return spec.factory()

    def default_version_for(self, schema_version: str) -> str:
        try:
            return self._defaults[schema_version]
        except KeyError:
            raise IncompatibleRendererError(
                f"No HTML renderer is the default for schema {schema_version!r}") from None

    def original_version_for(self, schema_version: str) -> str:
        try:
            return self._originals[schema_version]
        except KeyError:
            raise IncompatibleRendererError(
                f"No original HTML renderer is registered for schema {schema_version!r}") from None

    def get_original_for_schema(self, schema_version: str) -> tuple[str, HtmlRenderer]:
        """Historical renderer of a schema; independent of whatever is currently recommended."""
        return self.get_for_schema(self.original_version_for(schema_version), schema_version)

    def get_for_schema(self, renderer_version: str | None, schema_version: str) -> tuple[str, HtmlRenderer]:
        """(renderer_version, renderer) for ``schema_version``; ``None`` selects the schema's default."""
        version = renderer_version or self.default_version_for(schema_version)
        renderer = self.get(version)
        if not self.supports(version, schema_version):
            raise IncompatibleRendererError(
                f"Renderer {version!r} does not support FinalReport schema {schema_version!r}")
        return version, renderer


def build_default_final_report_schemas() -> FinalReportSchemaRegistry:
    registry = FinalReportSchemaRegistry()
    # FinalReport is the 1.1 model today. When 1.2 arrives, freeze this class under its own
    # module (final_report_v1_1) and register the new model next to it; nothing else changes.
    registry.register(FINAL_REPORT_SCHEMA_1_1, FinalReport)
    registry.register(FINAL_REPORT_SCHEMA_1_2, FinalReportV12)  # independent contract: 1.1 is untouched
    registry.register(FINAL_REPORT_SCHEMA_1_3, FinalReportV13)  # independent contract: 1.1 / 1.2 are untouched
    return registry


def build_default_html_renderers() -> HtmlRendererRegistry:
    registry = HtmlRendererRegistry()
    registry.register(HTML_RENDERER_V1, HtmlReportRenderer,
                      schema_versions=(FINAL_REPORT_SCHEMA_1_1,), default_for=(FINAL_REPORT_SCHEMA_1_1,),
                      original_for=(FINAL_REPORT_SCHEMA_1_1,))
    # html-v2 presents 1.2 only; 1.1 keeps html-v1 as both its original and its default renderer.
    registry.register(HTML_RENDERER_V2, HtmlReportRendererV2,
                      schema_versions=(FINAL_REPORT_SCHEMA_1_2,), default_for=(FINAL_REPORT_SCHEMA_1_2,),
                      original_for=(FINAL_REPORT_SCHEMA_1_2,))
    # html-v3 presents 1.3 only (original AND default); 1.1 / 1.2 keep html-v1 / html-v2 untouched.
    registry.register(HTML_RENDERER_V3, HtmlReportRendererV3,
                      schema_versions=(FINAL_REPORT_SCHEMA_1_3,), default_for=(FINAL_REPORT_SCHEMA_1_3,),
                      original_for=(FINAL_REPORT_SCHEMA_1_3,))
    return registry


final_report_schemas = build_default_final_report_schemas()
html_renderers = build_default_html_renderers()

