"""
WTForms for the Power BI Flask Embed application.
"""
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, SelectField, TextAreaField, SelectMultipleField
from wtforms.validators import DataRequired, Length, Optional


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
    empresas = SelectMultipleField("Empresas Asociadas", coerce=int, validators=[])
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
    submit = SubmitField("Crear Link Público")


class FuturaEmpresaForm(FlaskForm):
    """Form for reviewing/processing future empresas."""

    notas = TextAreaField("Notas", validators=[Length(max=500)])
    submit = SubmitField("Guardar")


class ClientePrivadoForm(FlaskForm):
    """Form for creating/editing private clients (deprecated - use EmpresaForm)."""

    nombre = StringField("Nombre del Cliente", validators=[DataRequired(), Length(max=200)])
    submit = SubmitField("Guardar")
