"""Optional example seeds for KLARA analytics skills.

Run manually with a configured Flask app context if you want sample global
skills. These examples are intentionally generic and should be adapted to each
real report before activation.
"""
from app import create_app, db
from app.models import AnalyticsSkill


EXAMPLE_SKILLS = [
    {
        "skill_key": "ventas_sucursales",
        "domain_key": "ventas",
        "title": "Ventas por sucursal",
        "description": "Preguntas de ventas nominales segmentadas por sucursal.",
        "priority": "normal",
        "enforcement_mode": "soft",
        "confidence_label": "draft",
        "routing_text": (
            "ventas por sucursal, ranking de sucursales, desempeno comercial, "
            "comparacion entre locales, dimensiones de sucursal"
        ),
        "content": (
            "Priorizar medidas canonicas de ventas existentes. Usar dimensiones de "
            "sucursal solo si el esquema confirma la tabla correspondiente."
        ),
        "metadata_json": {
            "canonical_measures": [],
            "required_schema_items": [],
            "preferred_tables": ["Sucursales"],
            "allowed_dimensions": ["Sucursales[sucursal]"],
            "constraints": ["No inventar nombres de sucursales ni columnas."],
        },
        "routing_json": {
            "trigger_terms": ["ventas", "sucursal", "local"],
            "example_questions": [],
            "intents": ["ranking", "breakdown"],
            "negative_triggers": [],
        },
        "validation_json": {
            "common_failure_modes": [],
            "validation_notes": [],
        },
    },
    {
        "skill_key": "variacion_mensual_ventas",
        "domain_key": "ventas",
        "title": "Variacion mensual de ventas",
        "description": "Comparaciones mes contra mes de ventas.",
        "priority": "normal",
        "enforcement_mode": "soft",
        "confidence_label": "draft",
        "routing_text": (
            "variacion mensual ventas, crecimiento mes contra mes, caida mensual, "
            "comparacion temporal de ventas, MoM"
        ),
        "content": (
            "Si existe una medida canonica de variacion mensual, priorizarla antes "
            "de recalcular la logica temporal manualmente."
        ),
        "metadata_json": {
            "canonical_measures": ["VarMensual Ventas"],
            "required_schema_items": [
                {"item_type": "measure", "item_name": "VarMensual Ventas"}
            ],
            "preferred_tables": [],
            "allowed_dimensions": [],
            "constraints": ["No recalcular variacion mensual manualmente si existe la medida canonica."],
        },
        "routing_json": {
            "trigger_terms": ["variacion mensual", "mes contra mes", "MoM"],
            "example_questions": [],
            "intents": ["comparison"],
            "negative_triggers": [],
        },
        "validation_json": {
            "common_failure_modes": [],
            "validation_notes": [],
        },
    },
    {
        "skill_key": "ticket_promedio",
        "domain_key": "ventas",
        "title": "Ticket promedio",
        "description": "Preguntas de ticket promedio y venta media.",
        "priority": "normal",
        "enforcement_mode": "soft",
        "confidence_label": "draft",
        "routing_text": (
            "ticket promedio, promedio por comprobante, importe medio, venta media, "
            "analisis de tickets"
        ),
        "content": (
            "Usar la medida canonica de ticket promedio si existe. No dividir ventas "
            "por conteos sin confirmar la granularidad del modelo."
        ),
        "metadata_json": {
            "canonical_measures": ["Ticket Promedio"],
            "required_schema_items": [
                {"item_type": "measure", "item_name": "Ticket Promedio"}
            ],
            "preferred_tables": [],
            "allowed_dimensions": [],
            "constraints": ["Confirmar la medida canonica antes de calcular promedios manuales."],
        },
        "routing_json": {
            "trigger_terms": ["ticket promedio", "venta media", "promedio por comprobante"],
            "example_questions": [],
            "intents": ["value"],
            "negative_triggers": [],
        },
        "validation_json": {
            "common_failure_modes": [],
            "validation_notes": [],
        },
    },
]


def seed_examples() -> int:
    created = 0
    for payload in EXAMPLE_SKILLS:
        exists = AnalyticsSkill.query.filter(
            AnalyticsSkill.skill_key == payload["skill_key"],
            AnalyticsSkill.report_id_fk.is_(None),
            AnalyticsSkill.empresa_id_fk.is_(None),
            AnalyticsSkill.dataset_id.is_(None),
        ).first()
        if exists is not None:
            continue
        db.session.add(AnalyticsSkill(**payload))
        created += 1
    db.session.commit()
    return created


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        print(f"Created {seed_examples()} example analytics skills.")
