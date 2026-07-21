# Skill Router V1

KLARA now has an optional vector router for curated analytics skills.

## Skill Embeddings vs Schema Embeddings

Skill embeddings route the user question to operational knowledge: domains, canonical measures, required schema objects and constraints.

Schema embeddings still retrieve the technical Power BI model context from `SchemaEmbedding`. The router does not replace schema retrieval and never generates DAX.

## AnalyticsSkill

Skills are stored in `analytics_skills` with:

- `skill_key`, `domain_key`, `title`
- `description`, `priority`, `enforcement_mode`, `confidence_label`
- one scope: global, empresa, dataset or report
- `routing_text` for short embedding-optimized routing text
- `content` for prompt injection when selected
- `metadata_json` for structured schema hints
- `routing_json` for trigger terms, example questions, intents and exclusions
- `validation_json` for common failure modes and administrative validation notes
- optional Voyage `embedding`

Valid scopes are mutually exclusive:

- global: no `report_id_fk`, `empresa_id_fk` or `dataset_id`
- empresa: only `empresa_id_fk`
- dataset: only `dataset_id`
- report: only `report_id_fk`

When several active skills share a `skill_key`, priority is:

`report > dataset > empresa > global`

## Metadata JSON

V1 supports:

```json
{
  "canonical_measures": ["VarMensual Ventas"],
  "required_schema_items": [
    {"item_type": "measure", "item_name": "VarMensual Ventas"},
    {"item_type": "table", "item_name": "Sucursales"}
  ],
  "preferred_tables": ["Sucursales"],
  "allowed_dimensions": ["Sucursales[sucursal]"],
  "constraints": ["No recalcular la variacion mensual manualmente."]
}
```

`required_schema_items` accepts only `measure` and `table` in V1. Columns should be represented by requiring their table.

## Routing JSON

`routing_json` is intentionally evolutive:

```json
{
  "trigger_terms": ["ventas", "facturacion", "ranking"],
  "example_questions": ["Cuanto se vendio en abril?"],
  "intents": ["value", "ranking"],
  "negative_triggers": ["ticket promedio"],
  "required_companion_skill_keys": ["calendario_base"]
}
```

`required_companion_skill_keys` declares direct companion skills required whenever the current skill is selected. Companions are resolved by backend using `skill_key` within the active turn scope (`report`, `dataset`, `empresa`, `global`) and the same precedence as normal routing: `report > dataset > empresa > global`. Missing companions are recorded as route metadata warnings but do not block the primary skill. V1 does not recursively expand companions of companions.

## Validation JSON

`validation_json` keeps administrative knowledge that may later feed evals:

```json
{
  "common_failure_modes": [
    {
      "issue": "Usar una medida ABC para ventas comunes.",
      "prevention": "Usar la medida canonica de ventas nominales."
    }
  ],
  "validation_notes": ["Confirmar que los importes correspondan al periodo solicitado."]
}
```

`priority`, `enforcement_mode` and `confidence_label` are columns because they are operational: the admin UI and future rollout rules can filter or sort by them without parsing JSON.

## Admin UI

Skills can be managed at:

`/admin/ai-config?tab=skills`

The UI supports create, edit, activate/deactivate and queue reindexing for one skill. Embedding refresh runs in background; if Voyage fails, the skill remains saved and can be reindexed later.

## Reindexing

From Flask shell:

```python
from app.services.skill_vector_service import reindex_active_skills
reindex_active_skills(force=True)
```

The seed file `seed_analytics_skills.py` contains optional generic examples. It is not run automatically.

## Modes

Disabled:

```text
SKILL_ROUTER_ENABLED=false
```

No routing calls are made and chat behavior remains equivalent to the previous flow.

Shadow:

```text
SKILL_ROUTER_ENABLED=true
SKILL_ROUTER_MODE=shadow
```

The router embeds, searches and records decisions/costs, but does not modify prompt, schema retrieval or DAX execution.

Soft:

```text
SKILL_ROUTER_ENABLED=true
SKILL_ROUTER_MODE=active
SKILL_ROUTER_HARD_ENFORCEMENT_ENABLED=false
```

Selected skills are injected into the prompt, required schema items are forced into the initial schema prefetch, and DAX validation warnings are recorded without blocking execution.

Hybrid selector shadow:

```text
SKILL_ROUTER_ENABLED=true
SKILL_ROUTER_MODE=shadow
SKILL_ROUTER_SELECTOR_ENABLED=true
SKILL_ROUTER_SELECTOR_MODE=shadow
```

The router still embeds and retrieves scoped candidates. The LLM selector sees only minimal candidate cards (`skill_id`, `skill_key`, `domain_key`, `scope`, `title`, `description`, `routing_text`) and records which skills it would choose, but the active decision remains the vector route.

Hybrid selector active:

```text
SKILL_ROUTER_ENABLED=true
SKILL_ROUTER_MODE=active
SKILL_ROUTER_HARD_ENFORCEMENT_ENABLED=true
SKILL_ROUTER_SELECTOR_ENABLED=true
SKILL_ROUTER_SELECTOR_MODE=active
```

The LLM selector chooses from the vector candidates. If selector confidence is below threshold or it returns `no_skill_match=true`, no skill is injected. If the selector provider fails, the router falls back to the vector decision.

Required companion skills are resolved by the backend after the vector or LLM selector decision. The selector skill card remains minimal and does not include `required_companion_skill_keys`.

Hard enforcement remains off by default. A route can become hard only when `SKILL_ROUTER_HARD_ENFORCEMENT_ENABLED=true`, metadata is complete and the first selected skill has `enforcement_mode` `hard` or `hard_candidate`. Vector hard routes also require score and margin thresholds. LLM selector hard routes require selector confidence to meet `SKILL_ROUTER_HARD_SCORE_THRESHOLD`.

## Flags

Recommended minimal `.env` for active skill routing:

```text
SKILL_ROUTER_ENABLED=true
SKILL_ROUTER_MODE=active
SKILL_ROUTER_HARD_ENFORCEMENT_ENABLED=true
SKILL_ROUTER_SELECTOR_ENABLED=true
SKILL_ROUTER_SELECTOR_MODE=active
```

Optional overrides and their code defaults:

```text
SKILL_ROUTER_ENABLED=false
SKILL_ROUTER_MODE=shadow
SKILL_ROUTER_CANDIDATE_LIMIT=8
SKILL_ROUTER_MAX_SELECTED_SKILLS=10
SKILL_ROUTER_RERANK_ENABLED=false
SKILL_ROUTER_SELECTOR_ENABLED=false
SKILL_ROUTER_SELECTOR_MODE=shadow
SKILL_ROUTER_SELECTOR_MODEL=claude-haiku-4-5-20251001
SKILL_ROUTER_SELECTOR_CANDIDATE_LIMIT=8
SKILL_ROUTER_SELECTOR_CONFIDENCE_THRESHOLD=0.70
SKILL_ROUTER_HARD_ENFORCEMENT_ENABLED=false
SKILL_ROUTER_HARD_SCORE_THRESHOLD=0.78
SKILL_ROUTER_HARD_MARGIN_THRESHOLD=0.12
SKILL_ROUTER_SOFT_SCORE_THRESHOLD=0.35
SKILL_ROUTER_MAX_SKILL_CHARS=20000
```

`SKILL_ROUTER_RERANK_ENABLED` remains available, but the LLM selector is the preferred final selection layer when `SKILL_ROUTER_SELECTOR_ENABLED=true`.

## Observability

The router records Langfuse spans for route resolution, query embedding, candidate search, optional rerank, optional LLM selector and schema merge. Route metadata is persisted in `AIUsageEvent.metadata_json`; vectors and full skill content are not stored in usage metadata. Companion metadata includes required, resolved and missing companion skill keys.

## Revert

Set `SKILL_ROUTER_ENABLED=false`. Existing skills can remain in the database because disabled mode does not call Voyage, load skills, alter schema context, alter prompts or validate DAX.
