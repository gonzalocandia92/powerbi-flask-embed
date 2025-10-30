# Solución a Problemas de Conexión a Base de Datos

## Problema
Las conexiones a la base de datos se pierden, causando errores visibles en la aplicación.

## Solución Implementada

### 1. Configuración del Pool de Conexiones
Se agregó configuración avanzada del pool de SQLAlchemy para detectar y manejar conexiones perdidas:

```python
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,                # Conexiones permanentes en el pool
    'pool_recycle': 3600,           # Reciclar conexiones después de 1 hora
    'pool_pre_ping': True,          # Verificar conexión antes de usarla
    'max_overflow': 20,             # Conexiones adicionales permitidas
    'pool_timeout': 30,             # Timeout para obtener conexión
    'connect_args': {
        'connect_timeout': 10       # Timeout de conexión inicial
    }
}
```

**Clave: `pool_pre_ping=True`**
- Verifica automáticamente si la conexión está activa antes de usarla
- Detecta conexiones perdidas/obsoletas/timeout
- Obtiene una nueva conexión si la actual no está disponible
- **Esto previene la mayoría de errores de conexión perdida**

### 2. Limpieza de Sesiones por Request
Se agregó un manejador que cierra sesiones al final de cada request:

```python
@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()
```

Esto garantiza que:
- Las conexiones se devuelven al pool correctamente
- No hay conexiones huérfanas
- Cada request tiene un ciclo de vida limpio

### 3. Mecanismo de Reintento Automático
Se creó un decorador `@retry_on_db_error` que reintenta operaciones automáticamente:

```python
@retry_on_db_error(max_retries=3, delay=1)
def my_route():
    # código que accede a la base de datos
```

Características:
- **Hasta 3 intentos** automáticos en caso de error de conexión
- **Backoff exponencial**: espera de 1s antes del 2do intento, 2s antes del 3er intento
- Limpia la sesión en cada reintento
- Registra advertencias en logs
- Aplicado a **todas las rutas** que acceden a la base de datos

### 4. Rutas Protegidas

El decorador se aplicó a todas las rutas:
- ✓ Login y autenticación
- ✓ Página principal (index)
- ✓ Todas las listas (tenants, clients, workspaces, reports, usuarios PBI, configs)
- ✓ Todos los formularios de creación
- ✓ Vistas de reportes públicas (`/p/<slug>`)
- ✓ Vistas de reportes privadas
- ✓ Carga de usuarios (user_loader)
- ✓ Creación de links públicos

**Total: 18/18 rutas protegidas**

## Flujo de una Request con Conexión Perdida

1. Usuario hace una request a cualquier ruta
2. Pool pre-ping detecta que la conexión está perdida
3. SQLAlchemy automáticamente obtiene una nueva conexión
4. Si aún falla, el decorador reintenta la operación
5. En cada reintento: rollback + remove + espera + retry
6. Usuario ve resultado correcto (sin error visible)
7. Al finalizar: teardown limpia la sesión

## Escenarios Cubiertos

✅ **Timeout de base de datos**
- pool_recycle previene timeouts reciclando conexiones cada hora
- pool_pre_ping detecta conexiones expiradas

✅ **Base de datos reiniciada**
- pool_pre_ping detecta la desconexión
- retry_on_db_error reintenta la operación

✅ **Network glitch temporal**
- retry_on_db_error con backoff exponencial maneja interrupciones temporales

✅ **Conexiones obsoletas/stale**
- pool_pre_ping detecta y reemplaza conexiones obsoletas

✅ **Pool agotado**
- max_overflow permite conexiones adicionales temporales
- pool_timeout da tiempo suficiente para obtener una conexión

## Configuración Recomendada

### Variables de Entorno
Asegúrate de tener configurada la variable `SQLALCHEMY_DATABASE_URI` en tu archivo `.env`:

```env
SQLALCHEMY_DATABASE_URI=postgresql://usuario:password@host:puerto/nombre_db
```

### Para PostgreSQL
Si usas PostgreSQL, considera también configurar en el servidor:
```sql
-- Opcional: ajustar timeout del servidor (por defecto es infinito)
ALTER DATABASE nombre_db SET statement_timeout = '30min';
```

## Logging y Monitoreo

Los errores de conexión se registran con nivel WARNING:
```
[WARNING] Error de conexión a la base de datos (intento 1/3): ...
```

Si ves estos mensajes frecuentemente:
1. Revisa la salud del servidor de base de datos
2. Considera aumentar `pool_recycle` si las conexiones expiran antes
3. Revisa la configuración de timeout del servidor de base de datos

## Impacto en Performance

- **Caso normal**: Sin impacto visible
  - pool_pre_ping agrega ~1ms por query (imperceptible)
  
- **Caso con conexión perdida**: 
  - Intento 1 falla → espera 1s → Intento 2
  - Intento 2 falla → espera 2s → Intento 3
  - Intento 3 falla → error (después de 3 intentos totales)
  - Tiempo máximo total de espera: ~3 segundos
  - **Usuario ve resultado correcto** en lugar de un error (en la mayoría de casos)

## Testing

Se incluyen tests que verifican:
- ✓ Configuración del pool correcta
- ✓ Mecanismo de retry funcional
- ✓ Todas las rutas protegidas
- ✓ Parámetros del pool en rangos recomendados

## Compatibilidad

- ✅ Compatible con PostgreSQL (recomendado)
- ✅ Compatible con MySQL/MariaDB
- ✅ Compatible con SQLite (aunque pool_pre_ping no es necesario)
- ✅ No requiere cambios en el código frontend
- ✅ No requiere cambios en la base de datos

## Conclusión

Esta solución proporciona **resiliencia automática** ante problemas de conexión a la base de datos sin requerir cambios en el código existente ni en la experiencia del usuario. Las conexiones perdidas se detectan y manejan transparentemente.
