"""Typed references to structured evidence (V1.5.1) and their deterministic, type-aware resolution.

Evidence has three different shapes and a reference must say which one it means:

    Fact        a scalar                     ("Ventas totales" = 15.520.540)       -> FactRef
    Series      a dimension + values         ("Ventas por sucursal")               -> SeriesRef      (a bar_chart)
    SeriesItem  ONE row of a series by label ("Ayacucho" in "Ventas por sucursal") -> SeriesItemRef  (a card / hero figure)

``FactRef != SeriesRef != SeriesItemRef`` is enforced twice: by the JSON schema the writer receives (a discriminated
``type`` field) and by ``EvidenceResolver``, which never "helps": a key that exists as another kind of evidence is an
error that SAYS so (and lists what exists), it is never accepted. There is no fuzzy matching: labels match exactly.

The resolver only reads the evidence the composer handed to the writer (``ComposedReportInput`` records). It is pure
(no DB, no renderer, no LLM). Adding another reference kind later (e.g. a table cell) = one more model in ``ValueRef`` /
``AnyRef`` and one resolver method; nothing else here changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Iterable, Literal, Mapping, Union

from pydantic import Field

from .composed_report import EvidenceRecord
from .evidence import EvidenceFact, EvidenceSeries, EvidenceSeriesItem
from .report_content import Key, _Strict

MAX_LISTED = 15


class FactRef(_Strict):
    """A scalar fact of an analysis' evidence. Use for KPI cards and the hero figure."""
    type: Literal["fact"]
    section_key: Key
    fact_key: Key


class SeriesRef(_Strict):
    """A whole series. Use ONLY as the data source of a ``bar_chart``."""
    type: Literal["series"]
    section_key: Key
    series_key: Key


class SeriesItemRef(_Strict):
    """One item of a series, selected by its EXACT label. Use for a card / hero figure about one row."""
    type: Literal["series_item"]
    section_key: Key
    series_key: Key
    label: Annotated[str, Field(min_length=1, max_length=200)]


# What a single displayed figure (card value, hero figure) may point at.
ValueRef = Annotated[Union[FactRef, SeriesItemRef], Field(discriminator="type")]

REF_TYPES = {"fact": FactRef, "series": SeriesRef, "series_item": SeriesItemRef}
_NAMES = {"fact": "FACT", "series": "SERIES", "series_item": "SERIES_ITEM"}


def infer_ref_type(raw: Any) -> str | None:
    """The kind of a raw ref dict whose ``type`` is missing, when its fields allow exactly one reading."""
    if not isinstance(raw, dict) or raw.get("type") is not None:
        return None
    if "fact_key" in raw and "series_key" not in raw:
        return "fact"
    if "series_key" in raw and "fact_key" not in raw:
        return "series_item" if "label" in raw else "series"
    return None


# ── inventory / diagnostics ────────────────────────────────────────────────────────────────

def _short(names: list[str]) -> str:
    shown = names[:MAX_LISTED]
    return ", ".join(shown) + (f" (+{len(names) - len(shown)} más)" if len(names) > len(shown) else "") if shown else "(ninguno)"


def inventory_text(record: EvidenceRecord) -> str:
    lines = [f"Evidencia {record.key!r}:",
             f"  Facts (escalares → FactRef): {_short([f.key for f in record.facts])}",
             f"  Series (listas → SeriesRef en un bar_chart): {_short([s.key for s in record.series])}"]
    for series in record.series[:6]:
        lines.append(f"    · {series.key}: etiquetas {_short([i.label for i in series.items])}")
    return "\n".join(lines)


GUIDANCE = ("Para mostrar la serie completa: SeriesRef (type 'series') dentro de un bar_chart. "
            "Para mostrar UN elemento de una serie (una sucursal, un medio de pago): SeriesItemRef "
            "(type 'series_item') con el label exacto. Para un valor escalar: FactRef (type 'fact').")


@dataclass
class Resolution:
    """Result of resolving one reference: the evidence, or a message the repair step can act on."""
    fact: EvidenceFact | None = None
    series: EvidenceSeries | None = None
    item: EvidenceSeriesItem | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def formatted(self) -> str | None:
        if self.fact is not None:
            return self.fact.formatted_value
        return self.item.formatted_value if self.item is not None else None


class EvidenceResolver:
    """Resolves typed refs against the records an item is authorised to use.

    ``authorized``: section keys with status ok. Inventories are printed once per evidence record per resolver,
    so the repair message stays readable when several refs point at the same analysis.
    """

    def __init__(self, records: Mapping[str, EvidenceRecord], authorized: Iterable[str]):
        self.records = records
        self.authorized = set(authorized)
        self._described: set[str] = set()

    # -- shared -------------------------------------------------------------------------
    def _record(self, section_key: str) -> tuple[EvidenceRecord | None, str | None]:
        record = self.records.get(section_key)
        if section_key not in self.authorized or record is None:
            return None, (f"la evidencia {section_key!r} no está autorizada para este componente; "
                          f"autorizadas: {_short(sorted(self.authorized))}.")
        return record, None

    def _inventory(self, record: EvidenceRecord) -> str:
        if record.key in self._described:
            return f"(inventario de {record.key!r} más arriba)"
        self._described.add(record.key)
        return inventory_text(record)

    def _wrong_type(self, record: EvidenceRecord, key: str, wanted: str, exists_as: str) -> str:
        return (f"{key!r} existe como {_NAMES[exists_as]} en {record.key!r}, no como {_NAMES[wanted]} "
                f"(tipo de referencia incorrecto). {GUIDANCE}\n{self._inventory(record)}")

    def _missing(self, record: EvidenceRecord, key: str, wanted: str) -> str:
        return f"no existe {key!r} como {_NAMES[wanted]} en {record.key!r}.\n{self._inventory(record)}"

    # -- by kind ------------------------------------------------------------------------
    def fact(self, ref: FactRef) -> Resolution:
        record, error = self._record(ref.section_key)
        if record is None:
            return Resolution(error=error)
        found = next((f for f in record.facts if f.key == ref.fact_key), None)
        if found is not None:
            return Resolution(fact=found)
        if any(s.key == ref.fact_key for s in record.series):
            return Resolution(error=self._wrong_type(record, ref.fact_key, "fact", "series"))
        return Resolution(error=self._missing(record, ref.fact_key, "fact"))

    def series(self, ref: SeriesRef) -> Resolution:
        record, error = self._record(ref.section_key)
        if record is None:
            return Resolution(error=error)
        found = next((s for s in record.series if s.key == ref.series_key), None)
        if found is not None:
            return Resolution(series=found)
        if any(f.key == ref.series_key for f in record.facts):
            return Resolution(error=self._wrong_type(record, ref.series_key, "series", "fact"))
        return Resolution(error=self._missing(record, ref.series_key, "series"))

    def series_item(self, ref: SeriesItemRef) -> Resolution:
        resolved = self.series(SeriesRef(type="series", section_key=ref.section_key, series_key=ref.series_key))
        if not resolved.ok:
            return resolved
        series = resolved.series
        matches = [i for i in series.items if i.label == ref.label]          # exact, case-sensitive: no fuzzy matching
        if not matches:
            return Resolution(error=(
                f"la serie {series.key!r} de {ref.section_key!r} no tiene un elemento con label exacto {ref.label!r}"
                + (" (la serie fue truncada: puede faltar ese elemento)" if series.truncated else "")
                + f"; etiquetas disponibles: {_short([i.label for i in series.items])}."))
        if len({(m.value, m.formatted_value) for m in matches}) > 1:
            return Resolution(error=f"el label {ref.label!r} es ambiguo en la serie {series.key!r}.")
        return Resolution(series=series, item=matches[0])

    def value(self, ref: Union[FactRef, SeriesItemRef]) -> Resolution:
        return self.fact(ref) if isinstance(ref, FactRef) else self.series_item(ref)

    # -- raw dicts (before Pydantic): kind-checked, with a precise message -------------------
    def value_from_raw(self, raw: Any, *, where: str) -> Resolution:
        """Resolve a raw ``value_ref`` / ``secondary_value_ref`` dict (FactRef or SeriesItemRef)."""
        return self._raw(raw, ("fact", "series_item"), where)

    def series_from_raw(self, raw: Any, *, where: str) -> Resolution:
        return self._raw(raw, ("series",), where)

    def _raw(self, raw: Any, allowed: tuple[str, ...], where: str) -> Resolution:
        kind = raw.get("type") if isinstance(raw, dict) else None
        kind = kind if kind is not None else infer_ref_type(raw)
        expected = " o ".join(f"{REF_TYPES[a].__name__} (type {a!r})" for a in allowed)
        if kind not in REF_TYPES:
            return Resolution(error=f"{where}: debe ser {expected}; falta un 'type' válido (fact | series | series_item).")
        if kind not in allowed:
            return Resolution(error=f"{where}: acá corresponde {expected}, no {REF_TYPES[kind].__name__}. {GUIDANCE}")
        try:
            ref = REF_TYPES[kind].model_validate({**raw, "type": kind})
        except ValueError:
            fields = ", ".join(REF_TYPES[kind].model_fields)
            return Resolution(error=f"{where}: {REF_TYPES[kind].__name__} inválida; campos requeridos: {fields}.")
        resolved = self.value(ref) if kind != "series" else self.series(ref)
        if not resolved.ok:
            resolved.error = f"{where}: {resolved.error}"
        return resolved
