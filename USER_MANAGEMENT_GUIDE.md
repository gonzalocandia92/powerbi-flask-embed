# Sistema de Gestión de Usuarios, Roles y Permisos

## Resumen de Implementación

Se ha implementado un sistema completo de gestión de usuarios, roles y permisos para administradores en la aplicación Sudata PBI. Este sistema permite a los administradores crear, editar, eliminar usuarios y asignar roles y permisos de forma centralizada.

## ¿Qué se implementó?

### 1. **Modelos de Base de Datos** (`app/models.py`)
- **User**: Modelo extendido con:
  - `is_active`: Campo booleano para activar/desactivar usuarios
  - `created_at` y `updated_at`: Timestamps para auditoría
  - `roles`: Relación many-to-many con `Role`
  - Métodos: `has_permission()`, `has_role()`
  
- **Role**: Nuevo modelo para agrupar permisos
  - `name`: Nombre único del rol
  - `description`: Descripción del rol
  - `permissions`: Relación many-to-many con `Permission`
  - Método: `has_permission()`
  
- **Permission**: Nuevo modelo para granularidad de permisos
  - `name`: Nombre único del permiso
  - `description`: Descripción del permiso

### 2. **Formularios** (`app/forms.py`)
- `UserForm`: Crear/editar usuarios (username, contraseña, estado, admin)
- `UserRoleForm`: Asignar roles a usuarios
- `RoleForm`: Crear/editar roles
- `PermissionForm`: Crear/editar permisos
- `RolePermissionForm`: Asignar permisos a roles

### 3. **Rutas y Controladores** (`app/routes/users.py`)
- **Gestión de Usuarios**:
  - `GET /admin/users/` - Listar usuarios
  - `GET /admin/users/new` - Crear usuario (formulario)
  - `POST /admin/users/new` - Guardar nuevo usuario
  - `GET /admin/users/<id>/edit` - Editar usuario (formulario)
  - `POST /admin/users/<id>/edit` - Guardar usuario editado
  - `POST /admin/users/<id>/delete` - Eliminar usuario
  - `POST /admin/users/<id>/toggle-status` - Activar/desactivar usuario
  - `GET /admin/users/<id>/assign-roles` - Asignar roles (formulario)
  - `POST /admin/users/<id>/assign-roles` - Guardar roles asignados

- **Gestión de Roles**:
  - `GET /admin/users/roles/` - Listar roles
  - `GET /admin/users/roles/new` - Crear rol (formulario)
  - `POST /admin/users/roles/new` - Guardar nuevo rol
  - `GET /admin/users/roles/<id>/edit` - Editar rol (formulario)
  - `POST /admin/users/roles/<id>/edit` - Guardar rol editado
  - `POST /admin/users/roles/<id>/delete` - Eliminar rol
  - `GET /admin/users/roles/<id>/assign-permissions` - Asignar permisos (formulario)
  - `POST /admin/users/roles/<id>/assign-permissions` - Guardar permisos asignados

- **Gestión de Permisos**:
  - `GET /admin/users/permissions/` - Listar permisos
  - `GET /admin/users/permissions/new` - Crear permiso (formulario)
  - `POST /admin/users/permissions/new` - Guardar nuevo permiso
  - `GET /admin/users/permissions/<id>/edit` - Editar permiso (formulario)
  - `POST /admin/users/permissions/<id>/edit` - Guardar permiso editado
  - `POST /admin/users/permissions/<id>/delete` - Eliminar permiso

### 4. **Plantillas** (`app/templates/admin/`)
- **Usuarios**:
  - `users/list.html` - Listado con búsqueda y acciones
  - `users/form.html` - Formulario crear/editar
  - `users/assign_roles.html` - Asignar roles a usuario

- **Roles**:
  - `roles/list.html` - Listado con búsqueda y acciones
  - `roles/form.html` - Formulario crear/editar
  - `roles/assign_permissions.html` - Asignar permisos a rol

- **Permisos**:
  - `permissions/list.html` - Listado con búsqueda y acciones
  - `permissions/form.html` - Formulario crear/editar

### 5. **Decoradores de Autorización** (`app/utils/decorators.py`)
- `@admin_required`: Verifica que el usuario sea administrador
- `@permission_required('permission_name')`: Verifica permiso específico

### 6. **Interfaz de Usuario**
- Nuevo menú "Administración" en navbar (solo para administradores)
- Enlaces rápidos a: Usuarios, Roles, Permisos
- Búsqueda y filtrado en listados
- Confirmación de eliminación con SweetAlert2
- Interfaces coherentes con el diseño existente

### 7. **Migración de Base de Datos** (`migrations/versions/add_roles_permissions_001.py`)
Crea las siguientes tablas:
- `permissions` - Tabla de permisos
- `roles` - Tabla de roles
- `user_role` - Tabla asociativa (muchos a muchos)
- `role_permission` - Tabla asociativa (muchos a muchos)
- Modifica tabla `users` agregando `is_active`, `created_at`, `updated_at`

## Cómo Usar

### 1. **Crear Permisos**
1. Navega a `Administración > Permisos`
2. Click en "Nuevo Permiso"
3. Ingresa:
   - Nombre (ej: `can_edit_reports`)
   - Descripción (opcional)
4. Guarda

### 2. **Crear Roles**
1. Navega a `Administración > Roles`
2. Click en "Nuevo Rol"
3. Ingresa:
   - Nombre del rol (ej: `Editor`)
   - Descripción (opcional)
4. Guarda
5. Asigna permisos al rol:
   - Click en el icono 🔒 en la fila del rol
   - Selecciona los permisos que deseas
   - Guarda

### 3. **Crear Usuarios**
1. Navega a `Administración > Usuarios`
2. Click en "Nuevo Usuario"
3. Ingresa:
   - Usuario (nombre único)
   - Contraseña
   - Confirmar Contraseña
   - Es Administrador (checkbox)
   - Activo (checkbox)
4. Guarda
5. Asigna roles al usuario:
   - Click en el icono 🛡️ en la fila del usuario
   - Selecciona los roles
   - Guarda

### 4. **Editar/Desactivar Usuarios**
- Click en ✏️ para editar información
- Click en ⏸️ para desactivar/activar usuario
- Click en 🗑️ para eliminar usuario

## Características de Seguridad

✓ **Validación de Entrada**: WTForms valida todos los formularios
✓ **CSRF Protection**: Flask-WTF protege contra ataques CSRF
✓ **Hash de Contraseñas**: Werkzeug.security para almacenamiento seguro
✓ **Control de Acceso**: Decoradores `@admin_required` y `@permission_required`
✓ **SQL Injection Protection**: SQLAlchemy ORM previene inyección SQL
✓ **Auditoría**: Campos `created_at` y `updated_at` rastrean cambios

## Próximos Pasos Recomendados

1. **Crear Datos Iniciales**: Ejecutar migraciones y crear permisos/roles iniciales
   ```bash
   flask db upgrade
   ```

2. **Definir Permisos**: Crear una lista de permisos según las necesidades
   - `can_manage_users`
   - `can_manage_reports`
   - `can_view_analytics`
   - `can_manage_empresas`
   - etc.

3. **Crear Roles Estándar**:
   - `Administrador` (todos los permisos)
   - `Editor` (editar reports y empresas)
   - `Revisor` (solo lectura)
   - etc.

4. **Integrar Permisos en Rutas**: Usar `@permission_required('permission_name')` en rutas existentes

5. **Actualizar Comandos CLI**: Agregar comando para crear admin inicial con roles

## Estructura de Código

```
app/
├── models.py                  # Modelos User, Role, Permission
├── forms.py                   # Formularios de usuario management
├── routes/
│   └── users.py              # Rutas de users management
├── utils/
│   └── decorators.py         # @admin_required, @permission_required
└── templates/admin/
    ├── users/
    │   ├── list.html
    │   ├── form.html
    │   └── assign_roles.html
    ├── roles/
    │   ├── list.html
    │   ├── form.html
    │   └── assign_permissions.html
    └── permissions/
        ├── list.html
        └── form.html
```

## Cambios en Modelos Existentes

### User Model
```python
- is_admin: Boolean (default=False, changed from True)
+ is_active: Boolean (default=True)
+ created_at: DateTime
+ updated_at: DateTime
+ roles: Relationship to Role (many-to-many)
+ has_permission(permission_name): Method
+ has_role(role_name): Method
```

## Notas Importantes

⚠️ **Migración**: La migración cambia el default de `is_admin` de True a False. Los usuarios existentes mantendrán su estado actual.

⚠️ **Permisos**: Actualmente, los permisos se asignan a roles pero se validan mediante `User.has_permission()`. Las rutas aún no utilizan estos permisos; eso se implementará en la siguiente fase.

✅ **Compatibilidad**: Sistema completamente compatible con la arquitectura existente. No se modificaron rutas ni modelos existentes (solo extensiones).

## Test y Validación

```bash
# Crear base de datos de prueba
export SQLALCHEMY_DATABASE_URI="sqlite:///test.db"
export SECRET_KEY="test-key"
export FERNET_KEY="[generated-fernet-key]"

# Ejecutar migraciones
flask db upgrade

# Crear usuario admin
flask create-admin
```

---

**Implementado por**: Sistema Automatizado
**Fecha**: 2026-06-29
**Versión**: 1.0.0
