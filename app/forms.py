"""
WTForms for the Power BI Flask Embed application.
"""
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    FieldList,
    Form,
    FormField,
    IntegerField,
    PasswordField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional


class LoginForm(FlaskForm):
    """Form for user authentication."""

    username = StringField('Usuario', validators=[DataRequired()])
    password = PasswordField('Contraseña', validators=[DataRequired()])
    remember = BooleanField('Recordarme')
    submit = SubmitField('Entrar')


class TenantForm(FlaskForm):
    """Form for creating/editing Azure AD tenants."""

    name = StringField("Nombre", validators=[DataRequired()])
    tenant_id = StringField("Tenant ID", validators=[DataRequired()])
    client = SelectField("Client (Azure AD)", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Guardar")


class ClientForm(FlaskForm):
    """Form for creating/editing Azure AD clients."""

    name = StringField("Nombre cliente", validators=[DataRequired()])
    client_id = StringField("Client ID", validators=[DataRequired()])
    client_secret = PasswordField("Client Secret (se cifrará)")
    submit = SubmitField("Guardar")


class WorkspaceForm(FlaskForm):
    """Form for creating/editing Power BI workspaces."""

    name = StringField("Nombre", validators=[DataRequired()])
    workspace_id = StringField("Workspace ID", validators=[DataRequired()])
    tenant = SelectField("Tenant", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Guardar")


class ReportForm(FlaskForm):
    """Form for creating/editing Power BI reports."""

    name = StringField("Nombre", validators=[DataRequired()])
    report_id = StringField("Report ID", validators=[DataRequired()])
    embed_url = StringField("Embed URL (opcional)")
    workspace = SelectField("Workspace", coerce=int, validators=[DataRequired()])
    usuario_pbi = SelectField("Usuario Power BI", coerce=int, validators=[DataRequired()])
    es_publico = BooleanField("Es Público (accesible sin autenticación)")
    es_privado = BooleanField("Es Privado (requiere autenticación de empresa)")
    chatbot_enabled = BooleanField("Habilitar chatbot KLARA en links públicos")
    show_dax_query = BooleanField("Mostrar consultas DAX en el chat")
    empresas = SelectMultipleField("Empresas Asociadas", coerce=int, validators=[])
    empresa_facturadora_id = SelectField(
        "Empresa facturadora AI",
        coerce=int,
        validators=[Optional()],
        choices=[],
    )
    filter_enabled = BooleanField("Acepta filtros en la API privada")
    filter_table = StringField("Nombre de tabla (modelo Power BI)", validators=[Optional(), Length(max=200)])
    filter_column = StringField("Nombre de campo/columna", validators=[Optional(), Length(max=200)])
    submit = SubmitField("Guardar")


class UsuarioPBIForm(FlaskForm):
    """Form for creating/editing Power BI user credentials."""

    nombre = StringField("Nombre identificador", validators=[DataRequired()])
    username = StringField("Usuario Power BI", validators=[DataRequired()])
    password = PasswordField("Contraseña Power BI", validators=[DataRequired()])
    submit = SubmitField("Guardar")


class EmpresaForm(FlaskForm):
    """Form for creating/editing empresas (formerly private clients)."""

    nombre = StringField("Nombre de la Empresa", validators=[DataRequired(), Length(max=200)])
    cuit = StringField("CUIT", validators=[Length(max=20)])
    submit = SubmitField("Guardar")


class PublicLinkForm(FlaskForm):
    """Form for creating public report links."""

    custom_slug = StringField(
        "Nombre personalizado para el link",
        validators=[DataRequired(), Length(max=120)]
    )
    allow_refresh = BooleanField('Permitir actualización de datos', default=False)
    submit = SubmitField("Crear Link")


class PublicUrlForm(FlaskForm):
    """Form for creating a public report from a Power BI URL."""

    url = StringField(
        "URL de Power BI",
        validators=[DataRequired(), Length(max=2000)]
    )
    submit = SubmitField("Analizar URL")


class PublicUrlWorkspaceForm(FlaskForm):
    """Form for creating a workspace during URL-based report creation."""

    workspace_name = StringField("Nombre del Workspace", validators=[DataRequired(), Length(max=200)])
    tenant = SelectField("Tenant", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Continuar")


class PublicUrlReportForm(FlaskForm):
    """Form for creating a report during URL-based report creation."""

    report_name = StringField("Nombre del Report", validators=[DataRequired(), Length(max=200)])
    usuario_pbi = SelectField("Usuario Power BI", coerce=int, validators=[DataRequired()])
    es_publico = BooleanField("Es Público", default=True)
    es_privado = BooleanField("Es Privado", default=False)
    submit = SubmitField("Continuar")


class PublicUrlLinkForm(FlaskForm):
    """Form for creating a public link during URL-based report creation."""

    link_name = StringField(
        "Nombre del Link Público",
        validators=[DataRequired(), Length(max=120)]
    )
    allow_refresh = BooleanField('Permitir actualización de datos', default=False)
    submit = SubmitField("Crear Link Público")


class FuturaEmpresaForm(FlaskForm):
    """Form for reviewing/processing future empresas."""

    notas = TextAreaField("Notas", validators=[Length(max=500)])
    submit = SubmitField("Guardar")


class ClientePrivadoForm(FlaskForm):
    """Form for creating/editing private clients (deprecated - use EmpresaForm)."""

    nombre = StringField("Nombre del Cliente", validators=[DataRequired(), Length(max=200)])
    submit = SubmitField("Guardar")


class BillingLimitForm(FlaskForm):
    """Form for global and company AI consumption limits."""

    limit_usd = DecimalField(
        "Limite USD",
        places=4,
        validators=[DataRequired(), NumberRange(min=0)],
    )
    cycle_anchor_day = IntegerField(
        "Dia de renovacion",
        validators=[DataRequired(), NumberRange(min=1, max=31)],
        default=1,
    )
    starts_at = DateField("Vigente desde", validators=[Optional()])
    ends_at = DateField("Vigente hasta", validators=[Optional()])
    is_active = BooleanField("Limite activo", default=True)
    submit = SubmitField("Guardar limite")


class AgentPromptConfigForm(FlaskForm):
    """Form for persisted agent prompt instructions."""

    title = StringField(
        "Titulo",
        validators=[DataRequired(), Length(max=200)],
    )
    instructions = TextAreaField(
        "Instrucciones adicionales",
        validators=[DataRequired()],
    )
    starts_at = DateField("Vigente desde", validators=[Optional()])
    ends_at = DateField("Vigente hasta", validators=[Optional()])
    is_active = BooleanField("Prompt activo", default=True)
    schema_retrieval_prompt = TextAreaField(
        "Mini prompt para embedding",
        validators=[Optional()],
    )
    schema_table_context_limit = IntegerField(
        "Tablas recuperadas",
        validators=[Optional(), NumberRange(min=1, max=20)],
    )
    schema_measure_context_limit = IntegerField(
        "Medidas recuperadas",
        validators=[Optional(), NumberRange(min=1, max=40)],
    )
    submit = SubmitField("Guardar prompt")


class AIModelPricingForm(FlaskForm):
    """Form for provider/model AI pricing records."""

    provider = StringField(
        "Proveedor",
        validators=[DataRequired(), Length(max=50)],
    )
    model = StringField(
        "Modelo",
        validators=[DataRequired(), Length(max=120)],
    )
    event_type = SelectField(
        "Tipo de evento",
        choices=[
            ("generation", "Chat / Generacion"),
            ("embedding", "Embedding"),
            ("rerank", "Rerank"),
        ],
        validators=[DataRequired()],
    )
    input_cost_per_million_usd = DecimalField(
        "Input / 1M USD",
        places=6,
        validators=[Optional(), NumberRange(min=0)],
    )
    output_cost_per_million_usd = DecimalField(
        "Output / 1M USD",
        places=6,
        validators=[Optional(), NumberRange(min=0)],
    )
    cache_write_cost_per_million_usd = DecimalField(
        "Cache write / 1M USD",
        places=6,
        validators=[Optional(), NumberRange(min=0)],
    )
    cache_read_cost_per_million_usd = DecimalField(
        "Cache read / 1M USD",
        places=6,
        validators=[Optional(), NumberRange(min=0)],
    )
    effective_from = DateField(
        "Vigente desde",
        validators=[DataRequired()],
    )
    effective_to = DateField("Vigente hasta", validators=[Optional()])
    is_active = BooleanField("Pricing activo", default=True)
    submit = SubmitField("Guardar pricing")


class RequiredSchemaItemForm(Form):
    """One schema object required by an analytics skill."""

    item_type = SelectField(
        "Tipo",
        choices=[
            ("", "Seleccionar tipo"),
            ("measure", "Medida"),
            ("table", "Tabla"),
        ],
        validators=[Optional()],
    )
    item_name = StringField("Nombre", validators=[Optional(), Length(max=255)])


class CommonFailureModeForm(Form):
    """One administrative validation warning for an analytics skill."""

    issue = StringField("Problema", validators=[Optional(), Length(max=500)])
    prevention = StringField("Prevencion", validators=[Optional(), Length(max=500)])


class AnalyticsSkillForm(FlaskForm):
    """Form for manually curated analytics routing skills."""

    skill_key = StringField(
        "Clave de skill",
        validators=[DataRequired(), Length(max=120)],
    )
    domain_key = StringField(
        "Dominio",
        validators=[DataRequired(), Length(max=120)],
    )
    title = StringField(
        "Titulo",
        validators=[DataRequired(), Length(max=200)],
    )
    description = TextAreaField("Descripcion", validators=[Optional()])
    priority = SelectField(
        "Prioridad",
        choices=[
            ("low", "Low"),
            ("normal", "Normal"),
            ("high", "High"),
        ],
        validators=[DataRequired()],
        default="normal",
    )
    enforcement_mode = SelectField(
        "Modo de enforcement",
        choices=[
            ("soft", "Soft"),
            ("hard_candidate", "Hard candidate"),
            ("hard", "Hard"),
        ],
        validators=[DataRequired()],
        default="soft",
    )
    confidence_label = SelectField(
        "Confianza",
        choices=[
            ("", "Sin confirmar"),
            ("draft", "Draft"),
            ("reviewed", "Reviewed"),
            ("confirmed", "Confirmed"),
        ],
        validators=[Optional()],
        default="",
    )
    scope_type = SelectField(
        "Scope",
        choices=[
            ("global", "Global"),
            ("empresa", "Empresa"),
            ("dataset", "Dataset"),
            ("report", "Reporte"),
        ],
        validators=[DataRequired()],
    )
    empresa_id = SelectField("Empresa", coerce=int, validators=[Optional()], choices=[])
    report_id = SelectField("Reporte", coerce=int, validators=[Optional()], choices=[])
    dataset_id = StringField("Dataset ID", validators=[Optional(), Length(max=200)])
    routing_text = TextAreaField("Texto de routing", validators=[DataRequired()])
    content = TextAreaField("Contenido operativo", validators=[DataRequired()])
    canonical_measures = TextAreaField("Medidas canonicas", validators=[Optional()])
    preferred_tables = TextAreaField("Tablas preferidas", validators=[Optional()])
    allowed_dimensions = TextAreaField("Dimensiones permitidas", validators=[Optional()])
    constraints = TextAreaField("Restricciones", validators=[Optional()])
    required_schema_items = FieldList(
        FormField(RequiredSchemaItemForm),
        min_entries=1,
    )
    trigger_terms = TextAreaField("Terminos disparadores", validators=[Optional()])
    example_questions = TextAreaField("Preguntas ejemplo", validators=[Optional()])
    intents = TextAreaField("Intenciones", validators=[Optional()])
    negative_triggers = TextAreaField("Disparadores negativos", validators=[Optional()])
    required_companion_skill_keys = TextAreaField("Skills companion requeridas", validators=[Optional()])
    common_failure_modes = FieldList(
        FormField(CommonFailureModeForm),
        min_entries=1,
    )
    validation_notes = TextAreaField("Notas de validacion", validators=[Optional()])
    is_active = BooleanField("Skill activa", default=True)
    submit = SubmitField("Guardar skill")


class UserForm(FlaskForm):
    """Form for creating/editing application users."""

    username = StringField("Usuario", validators=[DataRequired(), Length(min=3, max=120)])
    password = PasswordField("Contraseña", validators=[Optional(), Length(min=6)])
    password_confirm = PasswordField("Confirmar Contraseña", validators=[Optional()])
    is_admin = BooleanField("Es Administrador")
    is_active = BooleanField("Activo", default=True)
    submit = SubmitField("Guardar")


class UserRoleForm(FlaskForm):
    """Form for assigning roles to users."""

    roles = SelectMultipleField("Roles", coerce=int, validators=[])
    submit = SubmitField("Asignar Roles")


class RoleForm(FlaskForm):
    """Form for creating/editing roles."""

    name = StringField("Nombre del Rol", validators=[DataRequired(), Length(min=3, max=120)])
    description = StringField("Descripción", validators=[Optional(), Length(max=500)])
    submit = SubmitField("Guardar")


class PermissionForm(FlaskForm):
    """Form for creating/editing permissions."""

    name = StringField("Nombre del Permiso", validators=[DataRequired(), Length(min=3, max=120)])
    description = StringField("Descripción", validators=[Optional(), Length(max=500)])
    submit = SubmitField("Guardar")


class RolePermissionForm(FlaskForm):
    """Form for assigning permissions to roles."""

    permissions = SelectMultipleField("Permisos", coerce=int, validators=[])
    submit = SubmitField("Asignar Permisos")
