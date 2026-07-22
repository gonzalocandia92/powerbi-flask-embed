# Configuración WhatsApp — Lado KLARA (panel web)

Todo lo que hay que hacer dentro de la aplicación web para habilitar y gestionar el chatbot de WhatsApp por empresa.

---

## Cómo funciona el acceso

No se le pide al usuario que mande ningún código ni slug. El acceso es **gestionado completamente por el administrador** desde el panel. El flujo es:

1. Admin habilita WhatsApp para una empresa
2. Admin autoriza qué números de teléfono pueden hablar con qué reportes de esa empresa
3. El usuario escribe al número de WhatsApp → KLARA lo reconoce y responde automáticamente

---

## Paso 1 — Habilitar WhatsApp en una empresa

1. Ir a **Admin → Empresas → (empresa) → Detalle**
2. En el panel de **"Acciones Rápidas"**, hacer clic en el toggle **"Habilitar WhatsApp"**
3. El toggle queda en verde: `whatsapp_enabled = true`

> Desactivar el toggle corta el acceso al instante para todos los números de esa empresa — el próximo mensaje que manden recibe un aviso de "sin acceso".

---



## Paso 2 — Autorizar números de teléfono

1. En el detalle de la empresa, ir a la sección **"Números Asociados"**
2. Clic en **"Autorizar Número"**
3. Completar el formulario:
  - **Número:** en formato internacional sin `+` ni espacios  
   Ej: para `+54 9 362 429-7130` → escribir `5493624297130`
  - **Reportes:** seleccionar uno o varios reportes de la empresa a los que puede acceder (solo aparecen los que tienen KLARA activo)
4. Guardar



### Comportamiento según cantidad de reportes autorizados


| Reportes autorizados | Qué pasa cuando el número escribe    |
| -------------------- | ------------------------------------ |
| 1 reporte            | Conecta directo, sin menú            |
| 2+ reportes          | Muestra menú numerado para que elija |


El usuario puede escribir **"menú"** o **"cambiar"** en cualquier momento para volver a elegir reporte (no hace falta escribirlo exacto — alcanza con que la palabra aparezca en la frase).

---



## Paso 3 — Verificar que el reporte tenga KLARA activo

Para que un reporte aparezca como opción en el formulario de autorización y en el menú de WhatsApp, debe tener:

- `chatbot_enabled = true` — KLARA activado para ese reporte
- Un `PublicLink` activo — necesario para que el chatbot pueda operar

Esto se configura en **Admin → Reportes → (reporte) → Editar**.

---



## Paso 4 — Quitar acceso a un número

En el detalle de la empresa, sección **"Números Asociados"**:

- Clic en **"Quitar acceso"** al lado del número y reporte específico
- Si el número tenía ese reporte como activo, la próxima vez que escriba recibe el aviso de "sin acceso"

---



## Reglas de negocio importantes



### Revalidación en cada mensaje

El acceso no se cachea. En cada mensaje que llega, el sistema verifica:

- Que la empresa tenga `whatsapp_enabled = true`
- Que la empresa esté activa (`estado_activo = true`)
- Que el número siga teniendo ese reporte autorizado
- Que el reporte siga teniendo KLARA activo (`chatbot_enabled = true`)

Si algo cambió desde el último mensaje, el bot corta el acceso en el momento.

### Caso ambiguo: desactivar KLARA en un reporte

Si un usuario tiene 2 reportes autorizados y a uno le desactivás KLARA **después** de que el usuario ya estaba conectado a ese reporte, el próximo mensaje recibe "sin acceso" — no lo reconecta automáticamente al otro reporte disponible. El usuario tiene que volver a escribir para que el sistema lo redirigea al reporte válido.

---



## Widgets y elementos del panel



### Toggle WhatsApp (empresa)

- Ubicación: **Empresas → detalle → Acciones Rápidas**
- Estado verde = habilitado, gris = deshabilitado
- Efecto inmediato, sin necesidad de reiniciar nada



### Tabla "Números Asociados"

Columnas: **Número** / **Reporte** / **Fecha de autorización** / **Acción (Quitar acceso)**

### Formulario "Autorizar Número"

Campos:

- Número de teléfono (input, formato `549...`)
- Lista de reportes con KLARA activo (checkboxes)

---



## Flujo completo desde el punto de vista del usuario de WhatsApp

```
Usuario escribe al número de Sudata
    ↓
¿Está autorizado?
    No → "No tenés acceso habilitado. Contactá al administrador."
    Sí →
        ¿Tiene 1 reporte?
            Sí → "Listo, quedaste conectado al tablero de [Nombre]."
            No → Menú numerado con los reportes disponibles
                  ↓
                  Usuario elige un número
                  ↓
                  "Listo, quedaste conectado al tablero de [Nombre]."
        ↓
        Usuario hace preguntas → KLARA responde con los datos del tablero
        ↓
        Usuario escribe "menú" o "cambiar" → vuelve al menú de selección
```

---



## Checklist para dar de alta una empresa en WhatsApp

- [ ] Empresa creada en el panel (`Admin → Empresas`)
- [ ] WhatsApp habilitado en la empresa (toggle activo)
- [ ] Reporte(s) con `chatbot_enabled = true` y PublicLink activo
- [ ] Al menos un número autorizado cargado en "Números Asociados"
- [ ] Variables de entorno de Meta completadas en el servidor (ver doc `whatsapp-configuracion-meta.md`)
- [ ] Webhook de Meta apuntando al servidor y suscripto al campo `messages`