"""
Admin routes for managing users, roles, and permissions (CRUD operations).
"""
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from wtforms.validators import ValidationError

from app import db
from app.models import User, Role, Permission
from app.forms import UserForm, UserRoleForm, RoleForm, PermissionForm, RolePermissionForm
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
        
        # Validate passwords match
        if form.password.data and form.password.data != form.password_confirm.data:
            flash("Las contraseñas no coinciden", "danger")
            return render_template('admin/users/form.html', form=form, title='Nuevo Usuario', is_new=True)
        
        user = User(
            username=form.username.data,
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
        flash("Usuario creado exitosamente", "success")
        return redirect(url_for('users.list_users'))
    
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
        
        user.username = form.username.data
        user.is_admin = form.is_admin.data
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
    """Assign roles to a user."""
    user = User.query.get_or_404(user_id)
    form = UserRoleForm()
    
    # Populate role choices
    all_roles = Role.query.order_by(Role.name).all()
    form.roles.choices = [(r.id, r.name) for r in all_roles]
    
    if form.validate_on_submit():
        # Clear existing roles
        user.roles = []
        
        # Assign selected roles
        selected_roles = Role.query.filter(Role.id.in_(form.roles.data)).all()
        user.roles = selected_roles
        
        db.session.commit()
        logging.debug(f"Roles assigned to user: {user.username}")
        flash("Roles asignados correctamente", "success")
        return redirect(url_for('users.list_users'))
    
    # Pre-select current roles
    form.roles.data = [r.id for r in user.roles]
    
    return render_template('admin/users/assign_roles.html', form=form, user=user)


# ── Role Management Routes ────────────────────────────────────────────────────

@bp.route('/roles/')
@login_required
@admin_required
@retry_on_db_error(max_retries=3, delay=1)
def list_roles():
    """List all roles."""
    roles = Role.query.order_by(Role.name).all()
    return render_template('admin/roles/list.html', roles=roles, title='Roles')


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
