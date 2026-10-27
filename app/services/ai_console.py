"""Read-only view-models for the AI administration console.

Everything here is derived from data that is already persisted (reports, skills,
billing limits/usage, model catalog, evaluation cases...). Nothing in this module
writes to the database or introduces new tables: it only aggregates and shapes
information so the templates stay declarative.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import case, func
from sqlalchemy.orm import joinedload, selectinload

from app import db
from app.models import (
    AgentPromptConfig,
    AIModelGrant,
    AnalyticsSkill,
    BillingLimit,
    Empresa,
    ModelEvaluationCase,
    ModelEvaluationRun,
    Report,
)
from app.services import ai_billing


SCOPE_LABELS = {
    "global": "Global",
    "empresa": "Empresa",
    "report": "Reporte",
    "dataset": "Dataset",
}

# Readiness blockers (see ``model_catalog.model_readiness``) -> (label, tone).
BLOCKER_LABELS = {
    "credential_missing": ("Credencial faltante", "danger"),
    "pricing_missing": ("Pricing incompleto", "danger"),
    "profile_unknown": ("Perfil desconocido", "danger"),
    "profile_invalid": ("Perfil inválido", "danger"),
    "profile_unverified": ("Perfil sin verificar", "warning"),
    "validation_pending": ("Evaluación pendiente", "warning"),
    "disabled": ("Deshabilitado", "neutral"),
}
# Order in which a blocker becomes the headline status of a model.
BLOCKER_PRIORITY = (
    "disabled", "credential_missing", "pricing_missing", "profile_unknown",
    "profile_invalid", "validation_pending", "profile_unverified",
)


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _float(value) -> float:
    return float(value) if value is not None else 0.0


# --------------------------------------------------------------------------- #
# Report pickers
# --------------------------------------------------------------------------- #
def klara_enabled_reports() -> list[dict[str, Any]]:
    """Reports with KLARA enabled (``Report.chatbot_enabled``), never hiding incomplete ones.

    A report is flagged ``incomplete`` (with human readable ``issues``) when the
    inherited model policy (report -> empresa -> global grants) cannot serve chat:
    no grants, no allowed model, no default model or a disabled default model.
    """
    reports = (
        Report.query.filter(Report.chatbot_enabled.is_(True))
        .options(selectinload(Report.empresas))
        .order_by(Report.name.asc())
        .all()
    )
    grants_by_scope: dict[tuple[str, str | None], list[AIModelGrant]] = {}
    for grant in AIModelGrant.query.options(joinedload(AIModelGrant.model)).order_by(AIModelGrant.id.asc()).all():
        grants_by_scope.setdefault((grant.scope_type, grant.scope_id), []).append(grant)

    result = []
    for report in reports:
        empresa_id = ai_billing.resolve_report_billing_context(report).empresa_id
        chain = [("report", str(report.id))]
        if empresa_id is not None:
            chain.append(("empresa", str(empresa_id)))
        chain.append(("global", None))
        grants = next((grants_by_scope[key] for key in chain if grants_by_scope.get(key)), [])
        issues = _grant_issues(grants)
        result.append({
            "id": report.id,
            "name": report.name,
            "empresa_id": empresa_id,
            "incomplete": bool(issues),
            "issues": issues,
        })
    return result


def _grant_issues(grants: Iterable[AIModelGrant]) -> list[str]:
    grants = list(grants)
    if not grants:
        return ["Sin modelos permitidos para este alcance"]
    allowed = [grant for grant in grants if grant.allowed]
    if not allowed:
        return ["Ningún modelo permitido"]
    defaults = [grant for grant in allowed if grant.is_default]
    if not defaults:
        return ["Sin modelo predeterminado"]
    if not any(grant.model and grant.model.enabled for grant in defaults):
        return ["El modelo predeterminado está deshabilitado"]
    return []


def reports_with_skills() -> list[dict[str, Any]]:
    """Reports that own at least one skill of scope ``report`` (for the Skills selector)."""
    rows = (
        db.session.query(
            Report.id,
            Report.name,
            func.count(AnalyticsSkill.id),
            func.sum(case((AnalyticsSkill.is_active.is_(True), 1), else_=0)),
        )
        .join(AnalyticsSkill, AnalyticsSkill.report_id_fk == Report.id)
        .group_by(Report.id, Report.name)
        .order_by(Report.name.asc())
        .all()
    )
    return [
        {"id": row[0], "name": row[1], "skills_total": int(row[2] or 0), "skills_active": int(row[3] or 0)}
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
def _limit_row(*, label: str, scope: str, empresa_id: int | None, own_limit: BillingLimit | None,
               as_of: datetime) -> dict[str, Any]:
    effective = ai_billing.resolve_billing_limit(empresa_id=empresa_id, as_of=as_of)
    row: dict[str, Any] = {
        "label": label, "scope": scope, "empresa_id": empresa_id,
        "own_limit": own_limit, "effective": effective,
        "limit_usd": None, "spent_usd": 0.0, "available_usd": None, "percent": None,
        "origin": "none", "cycle_start": None, "cycle_end": None, "anchor_day": None,
        "state": "none", "state_label": "Sin límite", "tone": "neutral",
        "own_inactive": bool(own_limit is not None and not own_limit.is_active),
    }
    scope_id = str(empresa_id) if empresa_id is not None else None
    if effective is None:
        # Usage is still reported against the right billing scope, over the calendar month.
        return row
    window = ai_billing.monthly_anniversary_window(effective, as_of=as_of)
    spent = ai_billing.calculate_spend_decimal(
        scope_type="empresa" if scope == "empresa" else "global", scope_id=scope_id,
        cycle_start=window.cycle_start, cycle_end=window.cycle_end,
    )
    limit_usd = _float(effective.limit_usd)
    spent_usd = float(spent)
    percent = (spent_usd / limit_usd * 100) if limit_usd > 0 else (100.0 if spent_usd > 0 else 0.0)
    if percent >= 100:
        state, label, tone = "blocked", "Límite alcanzado", "danger"
    elif percent >= 80:
        state, label, tone = "warning", "Cerca del límite", "warning"
    else:
        state, label, tone = "ok", "En rango", "success"
    row.update(
        limit_usd=limit_usd, spent_usd=spent_usd, available_usd=max(limit_usd - spent_usd, 0.0),
        percent=percent, origin=effective.scope_type, cycle_start=window.cycle_start,
        cycle_end=window.cycle_end, anchor_day=window.anchor_day,
        state=state, state_label=label, tone=tone,
    )
    return row


def limits_overview(limits_by_scope: dict, companies: Iterable[Empresa]) -> dict[str, Any]:
    """KPIs + per-company rows for the consumption limits screen.

    ``limits_by_scope`` is the (scope_type, scope_id) -> latest ``BillingLimit`` map the
    route already builds. Effective limits and spend reuse the same helpers the billing
    enforcement uses, so what the admin sees is what the runtime enforces.
    """
    as_of = ai_billing.utcnow()
    global_limit = limits_by_scope.get(("global", None))
    rows = [
        _limit_row(
            label=company.nombre, scope="empresa", empresa_id=company.id,
            own_limit=limits_by_scope.get(("empresa", str(company.id))), as_of=as_of,
        )
        for company in companies
    ]
    global_row = _limit_row(label="Global (reportes sin empresa)", scope="global", empresa_id=None,
                            own_limit=global_limit, as_of=as_of)
    measured = [row for row in rows + [global_row] if row["limit_usd"] is not None]
    overrides = [row for row in rows if row["own_limit"] is not None and row["own_limit"].is_active]
    return {
        "rows": rows,
        "global_row": global_row,
        "kpis": {
            "global_limit": _float(global_limit.limit_usd) if global_limit is not None and global_limit.is_active else None,
            "spent": sum(row["spent_usd"] for row in measured),
            "available": sum(row["available_usd"] or 0.0 for row in measured),
            "overrides": len(overrides),
            "at_risk": sum(1 for row in rows if row["state"] in {"warning", "blocked"}),
        },
    }


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
EVENT_TYPE_LABELS = {"generation": "Chat / Generación", "embedding": "Embedding", "rerank": "Rerank"}


def pricing_filters(pricings: Iterable) -> dict[str, list]:
    pricings = list(pricings)
    return {
        "providers": sorted({item.provider for item in pricings if item.provider}),
        "event_types": [
            {"value": value, "label": EVENT_TYPE_LABELS.get(value, value)}
            for value in sorted({item.event_type for item in pricings if item.event_type})
        ],
    }


# --------------------------------------------------------------------------- #
# Model catalog
# --------------------------------------------------------------------------- #
def model_status(model, readiness: dict[str, Any]) -> dict[str, Any]:
    """Headline status + every blocker of a model, derived from ``model_readiness``."""
    blockers = list(readiness.get("blockers") or [])
    headline = next((key for key in BLOCKER_PRIORITY if key in blockers), None)
    chips = [
        {"key": key, "label": BLOCKER_LABELS.get(key, (key, "warning"))[0],
         "tone": BLOCKER_LABELS.get(key, (key, "warning"))[1]}
        for key in blockers
    ]
    if headline is not None:
        label, tone = BLOCKER_LABELS.get(headline, (headline, "warning"))
        group = "disabled" if headline == "disabled" else "blocked"
    elif readiness.get("ready_for_chat"):
        label, tone, group = "Listo", "success", "ready"
    else:
        # Fully configured and validated, but not offered to clients (client_selectable off).
        label, tone, group = "Listo (solo interno)", "info", "ready"
    return {"label": label, "tone": tone, "group": group, "blockers": chips,
            "ready_for_chat": bool(readiness.get("ready_for_chat"))}


def model_catalog_view(models: Iterable, readiness: dict) -> dict[str, Any]:
    rows = []
    for model in models:
        status = model_status(model, readiness[model.id])
        rows.append({"model": model, "readiness": readiness[model.id], "status": status})
    return {
        "rows": rows,
        "providers": sorted({row["model"].provider for row in rows if row["model"].provider}),
        "kpis": {
            "total": len(rows),
            "ready": sum(1 for row in rows if row["status"]["group"] == "ready"),
            "blocked": sum(1 for row in rows if row["status"]["group"] == "blocked"),
            "disabled": sum(1 for row in rows if row["status"]["group"] == "disabled"),
        },
    }


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def _prompt_in_force(config: AgentPromptConfig | None, now: datetime) -> bool:
    if config is None or not config.is_active:
        return False
    if config.starts_at and config.starts_at > now:
        return False
    if config.ends_at and config.ends_at < now:
        return False
    return True


def _latest_prompt(scope_type: str, scope_id: str | None) -> AgentPromptConfig | None:
    query = AgentPromptConfig.query.filter(AgentPromptConfig.scope_type == scope_type)
    query = query.filter(AgentPromptConfig.scope_id.is_(None)) if scope_id is None \
        else query.filter(AgentPromptConfig.scope_id == str(scope_id))
    return query.order_by(AgentPromptConfig.id.desc()).first()


def prompt_chain(scope_type: str, scope_id: int | str | None) -> dict[str, Any]:
    """Describe the additive prompt chain (protected base -> global -> empresa -> report).

    Prompts do not replace each other at runtime: ``resolve_agent_prompt_instructions``
    appends every active level, broadest first. The chain therefore lists each level with
    its state and the combined effective text.
    """
    now = _naive_utcnow()
    empresa = report = None
    if scope_type == "report" and scope_id is not None:
        report = db.session.get(Report, int(scope_id))
        if report is not None:
            empresa_id = ai_billing.resolve_report_billing_context(report).empresa_id
            empresa = db.session.get(Empresa, empresa_id) if empresa_id else None
    elif scope_type == "empresa" and scope_id is not None:
        empresa = db.session.get(Empresa, int(scope_id))

    levels: list[dict[str, Any]] = [{
        "key": "base", "label": "Prompt base protegido", "kind": "protected", "config": None,
        "in_force": True, "editable": False, "current": False,
        "note": "Reglas de seguridad, tools, sintaxis DAX y manejo de errores. No editable desde esta pantalla.",
    }]

    def add(key: str, label: str, config, *, editable: bool, current: bool, missing_note: str | None = None):
        in_force = _prompt_in_force(config, now)
        if config is None:
            kind, note = "empty", missing_note or "Sin configuración en este nivel."
        elif not in_force:
            kind, note = "inactive", "Existe pero no está vigente (inactivo o fuera de fechas)."
        else:
            kind, note = "active", None
        levels.append({"key": key, "label": label, "kind": kind, "config": config,
                       "in_force": in_force, "editable": editable, "current": current, "note": note})

    add("global", "Global", _latest_prompt("global", None), editable=scope_type == "global",
        current=scope_type == "global")
    if scope_type in {"empresa", "report"}:
        if empresa is not None:
            add("empresa", f"Empresa · {empresa.nombre}", _latest_prompt("empresa", str(empresa.id)),
                editable=scope_type == "empresa", current=scope_type == "empresa")
        elif scope_type == "report":
            levels.append({"key": "empresa", "label": "Empresa", "kind": "empty", "config": None,
                           "in_force": False, "editable": False, "current": False,
                           "note": "El reporte no resuelve una empresa única: este nivel no aplica."})
    if scope_type == "report" and report is not None:
        add("report", f"Reporte · {report.name}", _latest_prompt("report", str(report.id)),
            editable=True, current=True)

    effective = [level["config"] for level in levels if level["config"] is not None and level["in_force"]]
    return {
        "levels": levels,
        "effective": [{"label": level["label"], "title": level["config"].title,
                       "instructions": level["config"].instructions}
                      for level in levels if level["config"] is not None and level["in_force"]],
        "effective_count": len(effective),
        "empresa": empresa,
        "report": report,
    }


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
def skill_catalog_view(skills: Iterable[AnalyticsSkill]) -> dict[str, Any]:
    skills = list(skills)
    rows = []
    for skill in skills:
        scope = skill.scope
        if scope == "report":
            target_id, target = skill.report_id_fk, (skill.report.name if skill.report else f"Reporte #{skill.report_id_fk}")
        elif scope == "empresa":
            target_id, target = skill.empresa_id_fk, (skill.empresa.nombre if skill.empresa else f"Empresa #{skill.empresa_id_fk}")
        elif scope == "dataset":
            target_id, target = skill.dataset_id, skill.dataset_id
        else:
            target_id, target = None, "Todos los reportes"
        indexed = skill.embedding is not None and bool(skill.routing_document_hash)
        rows.append({
            "skill": skill, "scope": scope, "scope_label": SCOPE_LABELS.get(scope, scope),
            "target_id": target_id, "target": target, "indexed": indexed,
        })
    reports = reports_with_skills()
    companies = sorted(
        {(row["target_id"], row["target"]) for row in rows if row["scope"] == "empresa"},
        key=lambda item: str(item[1]).lower(),
    )
    active = [row for row in rows if row["skill"].is_active]
    indexed_active = [row for row in active if row["indexed"]]
    return {
        "rows": rows,
        "reports": reports,
        "companies": [{"id": cid, "name": name} for cid, name in companies],
        "domains": sorted({row["skill"].domain_key for row in rows if row["skill"].domain_key}),
        "kpis": {
            "total": len(rows),
            "active": len(active),
            "indexed": len(indexed_active),
            "pending": len(active) - len(indexed_active),
            "reports": len(reports),
            "scopes": len({row["scope"] for row in rows}),
        },
    }


# --------------------------------------------------------------------------- #
# Evaluations
# --------------------------------------------------------------------------- #
PROBLEM_REVIEWS = {"partial", "incorrect"}
PROBLEM_STATUSES = {"error", "interrupted"}


def _success_expr():
    return func.sum(case((ModelEvaluationCase.status == "success", 1), else_=0))


def _review_expr(value: str):
    return func.sum(case((ModelEvaluationCase.review_status == value, 1), else_=0))


def evaluation_model_comparison(run_id: int | None = None) -> list[dict[str, Any]]:
    """Per-model aggregates over the persisted evaluation cases (one run or all of them)."""
    c = ModelEvaluationCase
    query = db.session.query(
        c.model_key,
        func.count(c.id),
        _success_expr(),
        func.sum(case((c.status.in_(tuple(PROBLEM_STATUSES)), 1), else_=0)),
        func.sum(c.pipeline_total_cost),
        func.avg(c.latency_ms),
        func.sum(c.input_tokens),
        func.sum(c.output_tokens),
        func.sum(c.reasoning_tokens),
        func.sum(c.cache_read_tokens),
        func.avg(c.tool_rounds),
        _review_expr("correct"),
        _review_expr("partial"),
        _review_expr("incorrect"),
        func.count(func.distinct(c.run_id)),
    )
    if run_id is not None:
        query = query.filter(c.run_id == run_id)
    result = []
    for row in query.group_by(c.model_key).order_by(c.model_key.asc()).all():
        (model_key, cases, ok, errors, cost, latency, tok_in, tok_out, tok_reason, cache_read,
         tool_rounds, correct, partial, incorrect, runs) = row
        cases, ok, errors = int(cases or 0), int(ok or 0), int(errors or 0)
        correct, partial, incorrect = int(correct or 0), int(partial or 0), int(incorrect or 0)
        reviewed = correct + partial + incorrect
        cost = _float(cost)
        tok_in, cache_read = int(tok_in or 0), int(cache_read or 0)
        result.append({
            "model_key": model_key, "cases": cases, "runs": int(runs or 0),
            "success": ok, "errors": errors,
            "success_rate": (ok / cases * 100) if cases else None,
            "quality": ((correct + 0.5 * partial) / reviewed * 100) if reviewed else None,
            "reviewed": reviewed, "correct": correct, "partial": partial, "incorrect": incorrect,
            "pending_review": max(ok - reviewed, 0),
            "cost": cost, "cost_per_case": (cost / cases) if cases else None,
            "latency_ms": float(latency) if latency is not None else None,
            "input_tokens": tok_in, "output_tokens": int(tok_out or 0),
            "reasoning_tokens": int(tok_reason or 0), "cache_read_tokens": cache_read,
            "cache_ratio": (cache_read / tok_in * 100) if tok_in else None,
            "tool_rounds": float(tool_rounds) if tool_rounds is not None else None,
        })
    _mark_best(result)
    return result


def _mark_best(rows: list[dict[str, Any]]) -> None:
    """Flag the best value of each comparable metric so the table can highlight it."""
    def best(metric: str, *, lowest: bool):
        values = [row[metric] for row in rows if row.get(metric) is not None]
        if len(rows) < 2 or not values:
            return None
        return min(values) if lowest else max(values)

    targets = {
        "quality": best("quality", lowest=False), "success_rate": best("success_rate", lowest=False),
        "cost_per_case": best("cost_per_case", lowest=True), "latency_ms": best("latency_ms", lowest=True),
        "tool_rounds": best("tool_rounds", lowest=True),
    }
    for row in rows:
        row["best"] = {metric: target is not None and row.get(metric) == target
                       for metric, target in targets.items()}


def evaluation_runs_overview(runs: Iterable[ModelEvaluationRun]) -> dict[str, Any]:
    """Per-run quick stats (models, cost, errors, review progress) in a single grouped query."""
    runs = list(runs)
    ids = [run.id for run in runs]
    stats: dict[int, dict[str, Any]] = {}
    if ids:
        c = ModelEvaluationCase
        rows = db.session.query(
            c.run_id, func.count(func.distinct(c.model_key)), func.count(c.id), _success_expr(),
            func.sum(case((c.status.in_(tuple(PROBLEM_STATUSES)), 1), else_=0)),
            func.sum(c.pipeline_total_cost),
            func.sum(case((c.review_status != "unreviewed", 1), else_=0)),
        ).filter(c.run_id.in_(ids)).group_by(c.run_id).all()
        for run_id, models, cases, ok, errors, cost, reviewed in rows:
            stats[run_id] = {"models": int(models or 0), "cases": int(cases or 0), "success": int(ok or 0),
                             "errors": int(errors or 0), "cost": _float(cost), "reviewed": int(reviewed or 0)}
    empty = {"models": 0, "cases": 0, "success": 0, "errors": 0, "cost": 0.0, "reviewed": 0}
    per_run = {run.id: stats.get(run.id, empty) for run in runs}
    return {
        "per_run": per_run,
        "kpis": {
            "runs": len(runs),
            "completed": sum(1 for run in runs if run.status == "completed"),
            "active": sum(1 for run in runs if run.status in {"queued", "running", "cancel_requested"}),
            "cases": sum(item["cases"] for item in per_run.values()),
            "cost": sum(item["cost"] for item in per_run.values()),
            "errors": sum(item["errors"] for item in per_run.values()),
        },
    }


def case_is_problem(case_: ModelEvaluationCase) -> bool:
    return (case_.status in PROBLEM_STATUSES or case_.review_status in PROBLEM_REVIEWS
            or bool(case_.failure_reason) or bool(case_.error_message))


def evaluation_question_matrix(cases: Iterable[ModelEvaluationCase]) -> dict[str, Any]:
    """Questions x models grid so the same question can be compared across models."""
    cases = list(cases)
    models = list(dict.fromkeys(case_.model_key for case_ in cases))
    questions: dict[str, dict[str, Any]] = {}
    for case_ in cases:
        entry = questions.setdefault(case_.question, {"question": case_.question,
                                                      "index": case_.sequence_index, "cells": {}})
        entry["index"] = min(entry["index"], case_.sequence_index)
        entry["cells"].setdefault(case_.model_key, []).append(case_)
    rows = sorted(questions.values(), key=lambda item: item["index"])
    for row in rows:
        row["has_problem"] = any(case_is_problem(c) for group in row["cells"].values() for c in group)
        costs = [c.pipeline_total_cost for group in row["cells"].values() for c in group
                 if c.pipeline_total_cost is not None]
        row["cost_spread"] = (max(costs) - min(costs)) if len(costs) > 1 else 0
    return {"models": models, "rows": rows}
