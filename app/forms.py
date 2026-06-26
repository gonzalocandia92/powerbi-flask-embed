"""
WTForms for the Power BI Flask Embed application.
"""
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
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
