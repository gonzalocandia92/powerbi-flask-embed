# Integración de hotfix/reports

Se integró el delta de `hotfix/reports` hasta `dfc7d3c3799b498dfb06b2b493a65522e92e7f45`, preservando el worktree y la arquitectura actual de KLARA.

## Migraciones

La revisión histórica local `ebcd339309df_add_klara_catalog_baseline.py` permanece intacta. La revisión remota con el mismo identificador y otra ascendencia no se incorpora.

Las nuevas revisiones forman una sola cadena:

`b8c9d0e1f2a3 → 91af0b7c2d10 → 91af0b7c2d11 → 91af0b7c2d12`

- `91af0b7c2d10`: agrega el flag de restablecimiento a los enlaces como paso previo al traslado.
- Si la base ya tiene `public_links.allow_reset_to_default`, esta revisión conserva la columna y sus valores; la revisión siguiente los traslada a `reports`.
- `91af0b7c2d11`: traslada actualización y restablecimiento a `reports`, agrega actualización visual y elimina los dos flags de `public_links`. Modelo, formularios y rutas se actualizan conjuntamente.
- `91af0b7c2d12`: agrega login opcional, permiso `reports.read` y rol Lector de reportes. Los reportes existentes siguen abiertos por defecto.

Si los enlaces activos de un reporte tienen permisos diferentes, se habilita la acción a nivel del reporte cuando cualquiera la permitía, igual que en el hotfix. Los enlaces inactivos no determinan el valor. El downgrade replica el valor del reporte en todos sus enlaces; no recupera las diferencias originales. El downgrade del login elimina el rol, el permiso y sus asignaciones.

## Aplicación

La integración modifica archivos; no aplica migraciones ni reinicia servicios. Antes de actualizar la base real, realizar un respaldo y comprobar `flask db current` y `flask db heads` en el entorno correcto. Esta cadena está destinada a bases con el historial local de KLARA; no debe usarse directamente sobre una base que solo tenga el historial remoto del hotfix.

Después del respaldo y de verificar el historial, aplicar `flask db upgrade` junto con el despliegue del código actualizado. El código anterior no puede seguir funcionando contra la base después de quitar `public_links.allow_refresh`.

Se conservan catálogo, perfiles, billing, reporting, evaluaciones y sus migraciones. El control de acceso también cubre `/api/chatbot/models`, que existe en el worktree y no en el hotfix original.

Los cambios de proxy y Compose requieren recrear el gateway en el siguiente despliegue. El script `backup_db.py` y su modo dry-run se incorporan sin ejecutarlos contra la base configurada.

## Verificación

Se verificó el grafo Alembic con un único head y el upgrade/downgrade incremental en SQLite en memoria, conservando filas de catálogo, ejecuciones y billing. Las pruebas de acceso, acciones, API privada, CSRF y backups usan datos de prueba. También se validaron compilación Python, scripts JavaScript de reporting y `docker compose config --quiet`.

La suite ampliada detectó tres fallos preexistentes en `tests/test_analytics_integration.py`: dos por `visits.id` con BigInteger autoincremental en SQLite y uno por un login de prueba dirigido a `/auth/login` en lugar de `/login`. Se reprodujeron con los módulos originales de modelos y autenticación, sin modificar estos tests ni corregir comportamientos ajenos al hotfix.

La cadena completa histórica usa características de PostgreSQL; no se ejecutó sobre una base real durante esta integración.
