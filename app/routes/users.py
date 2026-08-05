"""
Admin routes for managing users, roles, and permissions (CRUD operations).
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from wtforms.validators import ValidationError

from app import db
from app.models import (
    Empresa,
    McpAgentConfig,
    McpConfigEmpresa,
    McpModelGrant,
    McpModelRole,
    Permission,
    Role,
    User,
    UserEmpresa,
)
from app.forms import UserForm, RoleForm, PermissionForm, RolePermissionForm
from app.utils.decorators import retry_on_db_error, admin_required

bp = Blueprint('users', __name__, url_prefix='/admin/users')


# ── User Management Routes ────────────────────────────────────────────────────

@bp.route('/')
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def list_users():
    """List all users."""
    users = User.query.order_by(User.username).all()
    return render_template('admin/users/list.html', users=users, title='Usuarios')


@bp.route('/new', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def create_user():
    """Create a new user."""
    form = UserForm()
    
    if form.validate_on_submit():
        # Check if username already exists
        existing = User.query.filter_by(username=form.username.data).first()
        if existing:
            flash("Ya existe un usuario con ese nombre", "danger")
            return render_template('admin/users/form.html', form=form, title='Nuevo Usuario', is_new=True)
        
        # Check if email already exists
        if form.email.data:
            existing_email = User.query.filter_by(email=form.email.data).first()
            if existing_email:
                flash("Ya existe un usuario con ese email", "danger")
                return render_template('admin/users/form.html', form=form, title='Nuevo Usuario', is_new=True)
        
        # Validate passwords match
        if form.password.data and form.password.data != form.password_confirm.data:
            flash("Las contraseñas no coinciden", "danger")
            return render_template('admin/users/form.html', form=form, title='Nuevo Usuario', is_new=True)
        
        user = User(
            username=form.username.data,
            email=form.email.data or None,
            is_admin=form.is_admin.data,
            is_active=form.is_active.data
        )
        
        if form.password.data:
            user.set_password(form.password.data)
        else:
            flash("Se requiere una contraseña para el nuevo usuario", "danger")
            return render_template('admin/users/form.html', form=form, title='Nuevo Usuario', is_new=True)
        
        db.session.add(user)
        db.session.commit()
        
        logging.debug(f"User created: {user.username} (ID: {user.id})")
        flash(
            "Usuario creado. Ahora asignale empresas, modelos y roles para habilitar su acceso MCP.",
            "success",
        )
        return redirect(url_for('users.manage_access', user_id=user.id))
    
    return render_template('admin/users/form.html', form=form, title='Nuevo Usuario', is_new=True)


@bp.route('/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def edit_user(user_id):
    """Edit an existing user."""
    user = User.query.get_or_404(user_id)
    form = UserForm(obj=user)
    
    if form.validate_on_submit():
        # Check if username already exists (excluding current user)
        existing = User.query.filter(User.username == form.username.data, User.id != user_id).first()
        if existing:
            flash("Ya existe otro usuario con ese nombre", "danger")
            return render_template('admin/users/form.html', form=form, title='Editar Usuario', user=user, is_new=False)
        
        # Check if email already exists (excluding current user)
        if form.email.data:
            existing_email = User.query.filter(User.email == form.email.data, User.id != user_id).first()
            if existing_email:
                flash("Ya existe otro usuario con ese email", "danger")
                return render_template('admin/users/form.html', form=form, title='Editar Usuario', user=user, is_new=False)
        
        user.username = form.username.data
        user.email = form.email.data or None
        user.is_active = form.is_active.data
        
        # Only update password if provided
        if form.password.data:
            if form.password.data != form.password_confirm.data:
                flash("Las contraseñas no coinciden", "danger")
                return render_template('admin/users/form.html', form=form, title='Editar Usuario', user=user, is_new=False)
            user.set_password(form.password.data)
        
        db.session.commit()
        logging.debug(f"User updated: {user.username} (ID: {user.id})")
        flash("Usuario actualizado", "success")
        return redirect(url_for('users.list_users'))
    
    return render_template('admin/users/form.html', form=form, title='Editar Usuario', user=user, is_new=False)


@bp.route('/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def delete_user(user_id):
    """Delete a user."""
    user = User.query.get_or_404(user_id)
    username = user.username
    
    db.session.delete(user)
    db.session.commit()
    
    logging.debug(f"User deleted: {username} (ID: {user_id})")
    flash(f"Usuario '{username}' eliminado", "success")
    return redirect(url_for('users.list_users'))


@bp.route('/<int:user_id>/toggle-status', methods=['POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def toggle_user_status(user_id):
    """Toggle user active status."""
    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()
    
    status = "activado" if user.is_active else "desactivado"
    logging.debug(f"User status toggled: {user.username} - {status}")
    flash(f"Usuario {status}", "success")
    return redirect(url_for('users.list_users'))


@bp.route('/<int:user_id>/assign-roles', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def assign_roles(user_id):
    """Compatibility redirect to the canonical access screen."""
    User.query.get_or_404(user_id)
    flash('Los roles ahora se administran junto con empresas y accesos MCP.', 'info')
    return redirect(url_for('users.manage_access', user_id=user_id))


def _access_view_data(user):
    backoffice_roles = Role.query.order_by(Role.name).all()
    model_roles = McpModelRole.query.order_by(McpModelRole.name).all()
    companies = Empresa.query.filter_by(estado_activo=True).order_by(Empresa.nombre).all()
    links = (
        McpConfigEmpresa.query
        .join(McpAgentConfig, McpAgentConfig.id == McpConfigEmpresa.config_id)
        .filter(McpAgentConfig.is_active.is_(True))
        .order_by(McpAgentConfig.dataset_name, McpAgentConfig.id)
        .all()
    )
    models_by_company = {company.id: [] for company in companies}
    for link in links:
        if link.empresa_id in models_by_company:
            models_by_company[link.empresa_id].append(link.config)

    memberships = {membership.empresa_id for membership in user.empresa_memberships}
    grants = {
        (grant.empresa_id, grant.config_id): grant
        for grant in user.mcp_grants
        if grant.is_active
    }
    return {
        'user': user,
        'backoffice_roles': backoffice_roles,
        'model_roles': model_roles,
        'companies': companies,
        'models_by_company': models_by_company,
        'membership_ids': memberships,
        'grant_by_pair': grants,
    }


@bp.route('/<int:user_id>/access', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def manage_access(user_id):
    """Manage backoffice roles, companies and company-scoped MCP grants together."""
    user = User.query.get_or_404(user_id)

    if request.method == 'POST':
        role_ids = {
            int(value) for value in request.form.getlist('backoffice_role_ids')
            if value.isdigit()
        }
        selected_roles = Role.query.filter(Role.id.in_(role_ids)).all() if role_ids else []
        user.roles = selected_roles
        user.is_admin = request.form.get('is_admin') == 'on'

        requested_company_ids = {
            int(value) for value in request.form.getlist('company_ids')
            if value.isdigit()
        }
        selected_companies = Empresa.query.filter(
            Empresa.id.in_(requested_company_ids),
            Empresa.estado_activo.is_(True),
        ).all() if requested_company_ids else []
        selected_company_ids = {company.id for company in selected_companies}

        memberships = {item.empresa_id: item for item in user.empresa_memberships}
        for empresa_id in selected_company_ids - set(memberships):
            db.session.add(UserEmpresa(user_id=user.id, empresa_id=empresa_id))
        for empresa_id in set(memberships) - selected_company_ids:
            db.session.delete(memberships[empresa_id])

        model_roles = {role.id: role for role in McpModelRole.query.all()}
        valid_links = (
            McpConfigEmpresa.query
            .join(McpAgentConfig, McpAgentConfig.id == McpConfigEmpresa.config_id)
            .filter(
                McpConfigEmpresa.empresa_id.in_(selected_company_ids),
                McpAgentConfig.is_active.is_(True),
            )
            .all()
        ) if selected_company_ids else []
        valid_pairs = {(link.empresa_id, link.config_id) for link in valid_links}
        existing_grants = {
            (grant.empresa_id, grant.config_id): grant for grant in user.mcp_grants
        }

        for pair, grant in existing_grants.items():
            if pair not in valid_pairs:
                grant.is_active = False

        for empresa_id, config_id in valid_pairs:
            value = request.form.get(f'grant_{empresa_id}_{config_id}', '0')
            role_id = int(value) if value.isdigit() else 0
            grant = existing_grants.get((empresa_id, config_id))
            if role_id not in model_roles:
                if grant is not None:
                    grant.is_active = False
                continue
            if grant is None:
                grant = McpModelGrant(
                    user_id=user.id,
                    empresa_id=empresa_id,
                    config_id=config_id,
                )
                db.session.add(grant)
            grant.role_id = role_id
            grant.is_active = True

        db.session.commit()
        logging.debug('Access matrix updated for user %s', user.username)
        flash('Accesos del usuario actualizados correctamente.', 'success')
        return redirect(url_for('users.manage_access', user_id=user.id))

    return render_template('admin/users/access.html', **_access_view_data(user))


# ── Role Management Routes ────────────────────────────────────────────────────

@bp.route('/roles/')
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def list_roles():
    """List backoffice and MCP model roles as clearly separated categories."""
    roles = Role.query.order_by(Role.name).all()
    model_roles = McpModelRole.query.order_by(McpModelRole.name).all()
    return render_template(
        'admin/roles/list.html',
        roles=roles,
        model_roles=model_roles,
        title='Roles y permisos',
    )


@bp.route('/roles/new', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def create_role():
    """Create a new role."""
    form = RoleForm()
    
    if form.validate_on_submit():
        # Check if role name already exists
        existing = Role.query.filter_by(name=form.name.data).first()
        if existing:
            flash("Ya existe un rol con ese nombre", "danger")
            return render_template('admin/roles/form.html', form=form, title='Nuevo Rol', is_new=True)
        
        role = Role(
            name=form.name.data,
            description=form.description.data
        )
        
        db.session.add(role)
        db.session.commit()
        
        logging.debug(f"Role created: {role.name} (ID: {role.id})")
        flash("Rol creado exitosamente", "success")
        return redirect(url_for('users.list_roles'))
    
    return render_template('admin/roles/form.html', form=form, title='Nuevo Rol', is_new=True)


@bp.route('/roles/<int:role_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def edit_role(role_id):
    """Edit an existing role."""
    role = Role.query.get_or_404(role_id)
    form = RoleForm(obj=role)
    
    if form.validate_on_submit():
        # Check if role name already exists (excluding current role)
        existing = Role.query.filter(Role.name == form.name.data, Role.id != role_id).first()
        if existing:
            flash("Ya existe otro rol con ese nombre", "danger")
            return render_template('admin/roles/form.html', form=form, title='Editar Rol', role=role, is_new=False)
        
        role.name = form.name.data
        role.description = form.description.data
        
        db.session.commit()
        logging.debug(f"Role updated: {role.name} (ID: {role.id})")
        flash("Rol actualizado", "success")
        return redirect(url_for('users.list_roles'))
    
    return render_template('admin/roles/form.html', form=form, title='Editar Rol', role=role, is_new=False)


@bp.route('/roles/<int:role_id>/delete', methods=['POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def delete_role(role_id):
    """Delete a role."""
    role = Role.query.get_or_404(role_id)
    role_name = role.name
    
    db.session.delete(role)
    db.session.commit()
    
    logging.debug(f"Role deleted: {role_name} (ID: {role_id})")
    flash(f"Rol '{role_name}' eliminado", "success")
    return redirect(url_for('users.list_roles'))


@bp.route('/roles/<int:role_id>/assign-permissions', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def assign_permissions(role_id):
    """Assign permissions to a role."""
    role = Role.query.get_or_404(role_id)
    form = RolePermissionForm()
    
    # Populate permission choices
    all_permissions = Permission.query.order_by(Permission.name).all()
    form.permissions.choices = [(p.id, p.name) for p in all_permissions]
    
    if form.validate_on_submit():
        # Clear existing permissions
        role.permissions = []
        
        # Assign selected permissions
        selected_permissions = Permission.query.filter(Permission.id.in_(form.permissions.data)).all()
        role.permissions = selected_permissions
        
        db.session.commit()
        logging.debug(f"Permissions assigned to role: {role.name}")
        flash("Permisos asignados correctamente", "success")
        return redirect(url_for('users.list_roles'))
    
    # Pre-select current permissions
    form.permissions.data = [p.id for p in role.permissions]
    
    return render_template('admin/roles/assign_permissions.html', form=form, role=role)


# ── Permission Management Routes ──────────────────────────────────────────────

@bp.route('/permissions/')
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def list_permissions():
    """List all permissions."""
    permissions = Permission.query.order_by(Permission.name).all()
    return render_template('admin/permissions/list.html', permissions=permissions, title='Permisos')


@bp.route('/permissions/new', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def create_permission():
    """Create a new permission."""
    form = PermissionForm()
    
    if form.validate_on_submit():
        # Check if permission name already exists
        existing = Permission.query.filter_by(name=form.name.data).first()
        if existing:
            flash("Ya existe un permiso con ese nombre", "danger")
            return render_template('admin/permissions/form.html', form=form, title='Nuevo Permiso', is_new=True)
        
        permission = Permission(
            name=form.name.data,
            description=form.description.data
        )
        
        db.session.add(permission)
        db.session.commit()
        
        logging.debug(f"Permission created: {permission.name} (ID: {permission.id})")
        flash("Permiso creado exitosamente", "success")
        return redirect(url_for('users.list_permissions'))
    
    return render_template('admin/permissions/form.html', form=form, title='Nuevo Permiso', is_new=True)


@bp.route('/permissions/<int:permission_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def edit_permission(permission_id):
    """Edit an existing permission."""
    permission = Permission.query.get_or_404(permission_id)
    form = PermissionForm(obj=permission)
    
    if form.validate_on_submit():
        # Check if permission name already exists (excluding current permission)
        existing = Permission.query.filter(Permission.name == form.name.data, Permission.id != permission_id).first()
        if existing:
            flash("Ya existe otro permiso con ese nombre", "danger")
            return render_template('admin/permissions/form.html', form=form, title='Editar Permiso', permission=permission, is_new=False)
        
        permission.name = form.name.data
        permission.description = form.description.data
        
        db.session.commit()
        logging.debug(f"Permission updated: {permission.name} (ID: {permission.id})")
        flash("Permiso actualizado", "success")
        return redirect(url_for('users.list_permissions'))
    
    return render_template('admin/permissions/form.html', form=form, title='Editar Permiso', permission=permission, is_new=False)


@bp.route('/permissions/<int:permission_id>/delete', methods=['POST'])
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def delete_permission(permission_id):
    """Delete a permission."""
    permission = Permission.query.get_or_404(permission_id)
    permission_name = permission.name
    
    db.session.delete(permission)
    db.session.commit()
    
    logging.debug(f"Permission deleted: {permission_name} (ID: {permission_id})")
    flash(f"Permiso '{permission_name}' eliminado", "success")
    return redirect(url_for('users.list_permissions'))
