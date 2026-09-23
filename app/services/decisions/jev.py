"""TypeSafe Jev skill selection; TypeSafe supplies probabilities, not routes."""
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any

import requests
from flask import has_app_context

from app.services.observability import start_observation
from app.services.skill_router import SkillSelectorDecision, build_skill_selector_card


SELECTION_RULES = [
    "Puede seleccionarse más de una skill.",
    "Una consulta puede contener varias intenciones independientes. Evaluar cada skill contra cada intención del usuario.",
    "No exigir que una skill cubra la consulta completa. Una skill puede ser correcta aunque cubra solamente una parte de una consulta compuesta.",
    "Seleccionar una skill si cubre directamente al menos una intención explícita de la pregunta del usuario.",
    "No seleccionar una skill solo porque sea semánticamente cercana o pertenezca al mismo dominio.",
    "Respetar especialmente las reglas, restricciones y exclusiones indicadas en routing_text.",
    "Si dos skills parecen similares, distinguirlas usando description y routing_text.",
    "No seleccionar análisis adicionales que el usuario no haya pedido.",
]


def _post_jev(payload: dict, timeout: int, api_key: str) -> dict:
    response = requests.post(
        "https://api.typesafe.ai/v1/systemone",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _probability(answer: Any, key: str) -> float:
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        raise ValueError(f"Invalid Noul answer for {key}")
    value = answer.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid probability for {key}")
    probability = float(value)
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError(f"Invalid probability for {key}")
    return probability


class JevSkillSelector:
    strategy = "jev"

    def __init__(self, metrics=None, transport=None):
        self.metrics = metrics if metrics is not None else {}
        self.transport = transport or _post_jev

    def get_skill_threshold(self, skill_id: int, settings) -> float:
        """Single override point for eventual per-skill thresholds."""
        del skill_id
        return settings.jev_threshold

    async def select(self, query, candidates, context) -> SkillSelectorDecision:
        settings = context["settings"]
        usage_totals = context.get("usage_totals")
        ai_usage_events = context.get("ai_usage_events")
        started = time.monotonic()
        cards = {f"skill_{int(item['skill'].id)}": build_skill_selector_card(item["skill"])
                 for item in candidates}
        state = {
            "user_message": query,
            "selection_policy": {
                "goal": "Seleccionar todas y solamente las skills necesarias para responder correctamente la solicitud del usuario.",
                "rules": SELECTION_RULES,
            },
            "candidate_skills": cards,
        }
        questions = {
            key: {
                "type": "noul",
                "instructions": (
                    "¿Existe al menos una intención, métrica o análisis solicitado "
                    "explícitamente en `user_message` cuya información necesaria "
                    f"esté cubierta directamente por `candidate_skills.{key}`, "
                    "considerando especialmente su `description` y `routing_text`?"
                ),
            }
            for key in cards
        }
        questions["no_skill_match"] = {
            "type": "noul",
            "instructions": (
                "¿Es cierto que ninguna de las skills presentes en `candidate_skills` "
                "cubre directamente ninguna intención, métrica o análisis solicitado "
                "explícitamente en `user_message`?"
            ),
        }
        payload = {"model": settings.jev_model, "state": state, "questions": questions}
        candidate_ids = [card["skill_id"] for card in cards.values()]
        with start_observation(
            name="select-skills-with-jev", as_type="generation", input=payload,
            model=settings.jev_model,
        ) as observation:
            try:
                api_key = os.getenv("TYPESAFE_API_KEY")
                if not api_key:
                    raise RuntimeError("TYPESAFE_API_KEY is not configured")
                pricing = None
                if has_app_context():
                    from app.services.ai_billing import resolve_pricing
                    pricing = resolve_pricing(
                        provider="typesafe", model=settings.jev_model, event_type="decision",
                    )
                response = await asyncio.wait_for(
                    asyncio.to_thread(self.transport, payload, settings.jev_timeout_seconds, api_key),
                    timeout=settings.jev_timeout_seconds + 1,
                )
                if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
                    raise ValueError("Invalid Jev response")
                answers = response["answers"]
                if set(answers) != set(questions):
                    raise ValueError("Jev answer keys do not match requested skills")
                probabilities = {str(card["skill_id"]): _probability(answers[key], key)
                                 for key, card in cards.items()}
                no_match_probability = _probability(answers["no_skill_match"], "no_skill_match")
                thresholds = {str(skill_id): self.get_skill_threshold(skill_id, settings)
                              for skill_id in candidate_ids}
                if any(not math.isfinite(value) or not 0 <= value <= 1 for value in thresholds.values()):
                    raise ValueError("Invalid Jev threshold")
                if not math.isfinite(settings.jev_no_match_threshold) or not 0 <= settings.jev_no_match_threshold <= 1:
                    raise ValueError("Invalid Jev no-match threshold")
                selected = sorted(
                    (skill_id for skill_id in candidate_ids
                     if probabilities[str(skill_id)] >= thresholds[str(skill_id)]),
                    key=lambda skill_id: (-probabilities[str(skill_id)], skill_id),
                )[:settings.max_selected_skills]
                selected_set = set(selected)
                rejected = sorted(skill_id for skill_id in candidate_ids if skill_id not in selected_set)
                no_match_confirmed = no_match_probability >= settings.jev_no_match_threshold
                conflict = bool(selected and no_match_confirmed)
                usage = response.get("usage")
                if not isinstance(usage, dict) or not isinstance(usage.get("input_tokens"), int):
                    raise ValueError("Jev response has no input token usage")
                input_tokens = usage["input_tokens"]
                output_tokens = usage.get("output_tokens", 0)
                if input_tokens < 0 or not isinstance(output_tokens, int) or output_tokens < 0:
                    raise ValueError("Invalid Jev token usage")
                actual_model = str(response.get("model") or settings.jev_model)
                details = {
                    "jev_probabilities": probabilities,
                    "jev_no_skill_match_probability": no_match_probability,
                    "thresholds_applied": thresholds,
                    "no_match_threshold": settings.jev_no_match_threshold,
                    "no_match_conflict": conflict,
                    "no_match_outcome": ("selected" if selected else
                                         "confirmed" if no_match_confirmed else "below_threshold"),
                    "requested_model": settings.jev_model,
                    "actual_model": actual_model,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
                decision = SkillSelectorDecision(
                    selected_skill_ids=selected, rejected_skill_ids=rejected,
                    confidence=(max(probabilities[str(item)] for item in selected)
                                if selected else no_match_probability),
                    no_skill_match=not selected and no_match_confirmed,
                    source="jev", details=details,
                )
                if usage_totals is not None:
                    usage_totals["input_tokens"] = int(usage_totals.get("input_tokens", 0)) + input_tokens
                    usage_totals["output_tokens"] = int(usage_totals.get("output_tokens", 0)) + output_tokens
                if ai_usage_events is not None:
                    ai_usage_events.append({
                        "provider": "typesafe", "model": settings.jev_model,
                        "event_type": "decision", "source_type": "skill_router_jev",
                        "trigger_type": "user_request", "operation_name": "select-skills-with-jev",
                        "status": "success", "input_tokens": input_tokens,
                        "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
                        "metadata_json": {"component": "skill_selector", "candidate_skill_ids": candidate_ids,
                                          **details, **decision.to_metadata()},
                    })
                if observation is not None:
                    update = {
                        "output": decision.to_metadata(),
                        "usage_details": {"input": input_tokens, "output": output_tokens},
                        "metadata": {"actual_model": actual_model},
                    }
                    if pricing is not None:
                        from app.services.ai_billing import calculate_cost_breakdown
                        costs = calculate_cost_breakdown(
                            pricing, input_tokens=input_tokens, output_tokens=output_tokens,
                        )
                        update["cost_details"] = {
                            "input": float(costs["input_cost_usd"]),
                            "output": float(costs["output_cost_usd"]),
                            "total": float(costs["total_cost_usd"]),
                        }
                        update["metadata"]["pricing_id"] = pricing.id
                    observation.update(**update)
                return decision
            except Exception as exc:
                details = {
                    "candidate_skill_ids": candidate_ids,
                    "requested_model": settings.jev_model,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "fallback_used": True,
                }
                decision = SkillSelectorDecision(
                    status="error", error_type="typesafe_provider_error" if isinstance(exc, requests.RequestException)
                    else "jev_invalid_response" if isinstance(exc, ValueError)
                    else "jev_unavailable",
                    no_skill_match=None, recoverable=True, failure_scope="secondary_component",
                    source="jev", details=details,
                )
                if ai_usage_events is not None:
                    ai_usage_events.append({
                        "provider": "typesafe", "model": settings.jev_model,
                        "event_type": "decision", "source_type": "skill_router_jev",
                        "trigger_type": "user_request", "operation_name": "select-skills-with-jev",
                        "status": "error", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                        "metadata_json": {"component": "skill_selector", "estimated_usage": True,
                                          "billable": False, **details, **decision.to_metadata()},
                    })
                if observation is not None:
                    observation.update(output=decision.to_metadata())
                return decision
            finally:
                self.metrics["skill_selector"] = self.metrics.get("skill_selector", 0) + round(
                    (time.monotonic() - started) * 1000
                )


class JevWithLLMFallbackSelector:
    strategy = "jev_with_llm_fallback"

    def __init__(self, jev_selector: JevSkillSelector, llm_selector):
        self.jev_selector = jev_selector
        self.llm_selector = llm_selector

    async def select(self, query, candidates, context) -> SkillSelectorDecision:
        jev_decision = await self.jev_selector.select(query, candidates, context)
        if jev_decision.status != "error":
            return jev_decision
        llm_decision = await self.llm_selector.select(query, candidates, context)
        llm_decision.details = {
            **jev_decision.details,
            **llm_decision.details,
            "fallback_used": True,
            "jev_error_type": jev_decision.error_type,
            "fallback_target": "llm_selector",
        }
        if llm_decision.status == "success":
            llm_decision.source = "llm_fallback"
        return llm_decision
