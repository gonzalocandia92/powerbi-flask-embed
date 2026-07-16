# WhatsApp Integration — Meta Cloud API

Integración de KLARA con WhatsApp Business mediante la Meta Cloud API oficial.

**Número activo:** `+54 9 11 2677-0450` — status `CONNECTED`. Phone Number ID y WABA ID en `.env` (`META_WA_PHONE_NUMBER_ID`), no se documentan acá por ser un repo público.

---

## 1. Arquitectura y flujo

El acceso ya **no** se basa en que el usuario mande el slug público del reporte. Un administrador pre-autoriza qué números pueden consultar qué reportes desde el panel de Empresas, y el bot resuelve todo automáticamente.

### 1.1 Diagrama de secuencia

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuario WhatsApp
    participant META as Meta Cloud API
    participant FW as Flask /webhook/whatsapp<br/>(app/routes/whatsapp.py)
    participant DB as Postgres<br/>(WhatsAppContact, WhatsAppAuthorizedNumber)
    participant CB as chatbot_service<br/>(mismo pipeline del chat web)
    participant LLM as Anthropic / MCP<br/>(agente + contexto del reporte)

    U->>META: Mensaje de WhatsApp
    META->>FW: POST /webhook/whatsapp<br/>(campo: messages)
    FW->>FW: Verificar X-Hub-Signature-256<br/>(HMAC con APP_SECRET)
    FW->>FW: _extract_incoming_message()<br/>from → phone_number, text, message_id
    FW->>META: POST /messages (status: read)<br/>marca mensaje como leído ✓✓
    FW->>DB: SELECT WhatsAppContact WHERE phone_number = ...

    alt Primer contacto (no existe WhatsAppContact)
        FW->>DB: SELECT WhatsAppAuthorizedNumber<br/>JOIN Empresa (whatsapp_enabled AND estado_activo)<br/>JOIN Report (chatbot_enabled)<br/>WHERE phone_number = ...
        alt Sin reportes autorizados
            FW->>META: "No tenes acceso habilitado..."
        else Un solo reporte autorizado
            FW->>DB: INSERT WhatsAppContact(phone_number, report_id_fk)
            FW->>META: "Listo, quedaste conectado al tablero de..."
        else Varios reportes autorizados
            FW->>DB: INSERT WhatsAppContact(phone_number, report_id_fk=NULL, awaiting_report_selection=true)
            FW->>META: Menu numerado con los reportes autorizados
        end

    else Comando "menu"/"cambiar"
        FW->>DB: Re-consulta autorizados (pueden haber cambiado)
        FW->>META: Reconecta directo (1 reporte) o muestra menu (varios)

    else awaiting_report_selection = true
        FW->>FW: Interpreta la respuesta como numero de opcion
        FW->>DB: UPDATE report_id_fk, awaiting_report_selection=false
        FW->>META: "Listo, quedaste conectado al tablero de..."

    else Contacto activo con reporte asignado
        FW->>DB: Revalida que el numero siga autorizado a ese reporte<br/>(empresa.whatsapp_enabled pudo cambiar)
        alt Ya no autorizado
            FW->>DB: DELETE WhatsAppContact
            FW->>META: "No tenes acceso habilitado..."
        else Lock ocupado (is_processing = true)
            FW->>META: "Todavia estoy respondiendo..."
        else Lock libre
            FW->>DB: UPDATE is_processing = true
            FW->>DB: Resuelve slug activo (PublicLink) del report_id_fk
            FW->>CB: procesar_interaccion_completa(<br/>text, slug, user_key="whatsapp:+numero", conversation_id)
            CB->>LLM: Consulta al agente (DAX / MCP / esquema del reporte)
            LLM-->>CB: Respuesta generada
            CB-->>FW: {answer, conversation_id}
            FW->>DB: UPDATE is_processing = false, conversation_id = ...
            FW->>META: answer
        end
    end
```

> `chatbot_service.procesar_interaccion_completa` sigue pidiendo un `slug` (no se tocó). `whatsapp.py` lo resuelve en runtime buscando el `PublicLink` activo del `report_id_fk` asignado — el admin nunca necesita compartir un slug con el usuario final.

---

## 2. Componentes

| Archivo                                          | Rol                                                                                     |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `app/routes/whatsapp.py`                         | Blueprint Flask: verificación de webhook (GET) y procesamiento de mensajes (POST)       |
| `app/services/meta_whatsapp_client.py`           | Cliente HTTP para Meta Graph API (envío de mensajes, mark as read)                      |
| `app/models.py` → `WhatsAppAuthorizedNumber`     | Tabla de autorización: qué número puede acceder a qué reporte, dentro de qué empresa     |
| `app/models.py` → `WhatsAppContact`              | Binding en vivo phone↔reporte activo + estado de selección de menú                       |
| `app/models.py` → `Empresa.whatsapp_enabled`     | Toggle admin: habilita/deshabilita el chat de WhatsApp para toda la empresa              |
| `app/routes/empresas.py`                         | Endpoints admin: `toggle_whatsapp`, `add_whatsapp_number`, `remove_whatsapp_number`      |
| `app/templates/admin/empresas/detail.html`       | Panel: toggle de WhatsApp + sección "Números Asociados"                                 |
| `app/templates/admin/empresas/whatsapp_add_number.html` | Formulario para autorizar un número a uno o varios reportes de la empresa         |
| `migrations/versions/7f3a9c2e1b6d_*`             | Migración original que crea `whatsapp_contacts`                                         |
| `migrations/versions/b3d4e5f6a7c8_*`             | Migración que agrega `whatsapp_authorized_numbers` y `Empresa.whatsapp_enabled`          |

### Modelo `WhatsAppAuthorizedNumber`

```
phone_number    VARCHAR(30)  — número en formato wa_id (ej: 5493624297130)
empresa_id_fk   FK → clientes_privados.id
report_id_fk    FK → reports.id
created_at      DATETIME

UNIQUE(phone_number, report_id_fk)
```

Un número puede tener varias filas (una por reporte autorizado), pero siempre dentro de la misma empresa.

### Modelo `WhatsAppContact` (actualizado)

```
phone_number              VARCHAR(30) UNIQUE — número en formato wa_id
report_id_fk              FK → reports.id, NULLABLE (null mientras elige del menú)
awaiting_report_selection BOOLEAN — true cuando se le mostró el menú y falta que elija
conversation_id           FK → chat_sessions.id (nullable, se llena tras la primera consulta)
is_processing             BOOLEAN — lock optimista para evitar respuestas duplicadas
created_at                DATETIME
last_message_at           DATETIME
```

> El campo `slug` que existía antes fue eliminado — ya no se usa para el registro.

### Reglas de acceso

- Un número puede tener acceso a uno o varios reportes de una misma empresa.
- **Un solo reporte autorizado** → conecta directo, sin menú.
- **Varios reportes** → menú numerado; el comando `menu` o `cambiar` lo vuelve a mostrar en cualquier momento. La detección no exige el mensaje exacto: alcanza con que "menu" o "cambiar" aparezca como palabra suelta en cualquier frase (ej. "quiero cambiar de tablero, me equivoque" también dispara el menú) — `_is_menu_command()` en `app/routes/whatsapp.py`.
- **No autorizado** → mensaje genérico, sin pistas de qué reportes existen.
- El acceso también depende de `Empresa.whatsapp_enabled` **y** `Empresa.estado_activo` — se revalida en **cada mensaje**, no solo al conectar, así que desactivar el toggle corta el acceso al instante (el próximo mensaje del número recibe el aviso de "sin acceso" y se borra su `WhatsAppContact`).
- **El reporte también debe tener `Report.chatbot_enabled = True` (KLARA activo)**. Un reporte autorizado pero sin KLARA activo queda invisible por WhatsApp: no aparece en el menú, y si es el único reporte cargado, el número recibe "sin acceso" aunque tenga una fila en `WhatsAppAuthorizedNumber`. El formulario "Autorizar Número" en el panel admin ya filtra esto — solo deja elegir reportes que tienen KLARA activo.
  > **Caso borde:** si un número tiene 2 reportes autorizados y a uno le desactivan KLARA *después*, y el usuario justo estaba conectado a ese reporte, el próximo mensaje lo desconecta con "no tenés acceso" en vez de reconectarlo automáticamente al otro reporte que sigue siendo válido — tiene que volver a escribir para que lo reconecte.

---

## 3. Panel de administración

En **Empresas → detalle de una empresa**:

- **Acciones Rápidas → Habilitar/Deshabilitar WhatsApp**: togglea `Empresa.whatsapp_enabled`.
- **Números Asociados**: lista los números autorizados y su reporte. "Autorizar Número" abre un formulario para cargar un número nuevo y elegir uno o varios reportes de esa empresa (checkboxes). "Quitar acceso" borra la fila de `WhatsAppAuthorizedNumber` para ese reporte puntual.

No hace falta tocar la base de datos a mano para dar de alta un número — todo el flujo pasa por este panel.

---

## 4. Variables de entorno

| Variable                     | Descripción                                                                   |
| ----------------------------- | ------------------------------------------------------------------------------ |
| `META_WA_PHONE_NUMBER_ID`    | ID del número de teléfono en Meta                                             |
| `META_WA_ACCESS_TOKEN`       | Bearer token para la Graph API (System User permanente, no vence)             |
| `META_WA_VERIFY_TOKEN`       | Token de verificación del webhook (elegido libremente)                        |
| `META_WA_APP_SECRET`         | App Secret de la app Meta (para verificar firma HMAC del webhook)             |
| `META_WA_TEST_MODE`          | `true` en desarrollo — normaliza números argentinos al formato legacy de Meta |

En producción: `META_WA_TEST_MODE=false`.

---

## 5. Setup inicial en Meta Developers

1. Crear app tipo **Business** en [developers.facebook.com](https://developers.facebook.com)
2. Agregar producto **WhatsApp** → vincular al **WhatsApp Business Account (WABA)**
3. Configurar webhook:
   - **Callback URL:** `https://<dominio>/webhook/whatsapp`
   - **Verify Token:** valor de `META_WA_VERIFY_TOKEN`
   - **Campo suscripto:** `messages`
4. Suscribir la app al WABA (una sola vez):
   ```bash
   curl -X POST "https://graph.facebook.com/v20.0/<WABA_ID>/subscribed_apps" \
     -H "Authorization: Bearer <ACCESS_TOKEN>"
   ```
5. **Solo aplica al número de prueba gratuito (sandbox)**: mientras uses el número que Meta te da al crear la app, únicamente los destinatarios agregados como *tester* (**WhatsApp → API Setup → Send and receive messages → Manage phone number list → Add recipient phone number**) pueden mandar/recibir mensajes reales. Esto es independiente de "App roles → Testers" (ese es para gente que accede al dashboard, no para destinatarios de mensajes).
6. **Con un número real y verificado conectado al WABA (como `+54 9 11 2677-0450`, status `CONNECTED`) esta restricción no aplica** — puede enviar y recibir mensajes de cualquier usuario de WhatsApp sin importar si la app está en modo Development o Live. Confirmado en producción: usuarios autorizados que nunca fueron agregados como tester recibieron respuesta normalmente.
   - **Publicar la app** (Business Verification + toggle a Live) NO es un requisito para este caso de uso (mensajería con tu propio número, tu propio negocio). Es relevante solo para permisos avanzados de Graph API o si la app fuera a gestionar WABAs de otros negocios.

---

## 6. Cómo autorizar un número (flujo actual)

Ya no se manda ningún slug por WhatsApp. El alta la hace un admin desde el panel:

1. Ir a **Empresas → (empresa) → Acciones Rápidas → Habilitar WhatsApp** (si no estaba habilitado).
2. En **Números Asociados → Autorizar Número**, cargar el número en formato internacional sin `+` ni espacios (ej: `5493624297130`) y marcar el/los reporte(s) a los que debe tener acceso.
3. Ese número ya puede escribirle a KLARA — sin menú si tiene un solo reporte, con menú numerado si tiene varios.

---

## 7. Testing local con ngrok

```powershell
# 1. Levantar Flask
docker compose up -d flask-powerbi

# 2. Túnel HTTPS (en otra terminal)
ngrok http 2052
# → anota la URL pública que te asigna

# 3. Registrar/actualizar el webhook en Meta Developers con esa URL
# Callback URL: https://<url-de-ngrok>/webhook/whatsapp

# 4. Suscribir app al WABA (si no estaba suscripta)
curl -X POST "https://graph.facebook.com/v20.0/<WABA_ID>/subscribed_apps" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

> **Nota:** con un dominio de ngrok reservado (plan pago o `--domain`), la URL no cambia entre reinicios y no hace falta re-registrar el webhook cada vez. Con el plan gratuito sin dominio reservado, la URL cambia cada sesión.
>
> **Riesgo operativo:** este setup depende de que la máquina local, Docker y el proceso de ngrok sigan corriendo. Si el túnel se cae, Meta devuelve `ERR_NGROK_3200` y el webhook queda mudo — sin ningún error visible del lado de la app (porque el request nunca llega). Antes de dar por roto el código, comprobar primero que el túnel esté vivo (`curl` directo a la Callback URL configurada en Meta).

---

## 8. Bugs conocidos y soluciones

### 8.1 `#131030` Recipient phone number not in allowed list (modo test)

**Síntoma:** El bot recibe el mensaje correctamente pero falla al responder.

**Causa:** En modo desarrollo, Meta solo permite enviar mensajes a números previamente autorizados. Los números argentinos se registran en el formato legacy (`54XXX15XXXXXXX`) pero el webhook los entrega en formato moderno (`549XXXXXXXXX`).

**Fix:** `META_WA_TEST_MODE=true` activa la normalización en `meta_whatsapp_client._normalize_ar_number()`. En producción, Meta resuelve ambos formatos automáticamente.

### 8.2 Webhooks duplicados → "Todavía estoy respondiendo" falso

**Síntoma:** El usuario recibe "Todavía estoy respondiendo tu mensaje anterior" aunque el bot ya había respondido correctamente.

**Causa:** Meta reintenta la entrega del webhook si el servidor no responde en menos de ~20 s (el LLM puede tardar más). La segunda entrega llega mientras `is_processing = true`, disparando el mensaje de espera.

**Fix:** Cache en memoria por `message_id` con TTL de 120 s. Si el mismo `message_id` llega por segunda vez, se devuelve 200 OK sin procesar (`_is_duplicate()` en `app/routes/whatsapp.py`).

> **Nota prod:** el cache es por proceso Flask. Con múltiples instancias, reemplazar por Redis.

### 8.3 Webhooks no llegan (silencioso)

**Síntoma:** El webhook GET de verificación funciona pero no llegan POST de mensajes.

**Causa posible 1:** La app no estaba suscripta al WABA (ver §5, paso 4).

**Causa posible 2:** El túnel de ngrok está caído (`ERR_NGROK_3200`) — ver nota de §7. Este fue el motivo real la última vez que pasó: el código y la base estaban bien, pero no había nada escuchando en la URL pública configurada en Meta.

**Fix:** confirmar primero con un `curl` directo a la Callback URL antes de sospechar del código.

### 8.4 Errores `DatasetExecuteQueriesError` en los logs (no es un bug)

**Síntoma:** Aparecen errores de Power BI tipo "Function SUMMARIZECOLUMNS expects a column name..." en los logs durante una consulta de WhatsApp.

**Causa:** Es el comportamiento normal del agente autocorrigiendo DAX — genera una consulta, Power BI la rechaza por sintaxis, el agente ajusta y reintenta según las reglas de su prompt hasta lograr una consulta válida. No indica un problema de infraestructura ni del código de WhatsApp.

**Cuándo preocuparse:** si el agente agota los reintentos y responde "tuve un problema" de forma repetida para la misma pregunta — ahí conviene revisar el modelo semántico del reporte, no el webhook.

---

## 9. Decisiones pendientes para producción

- [x] Reemplazar token temporal por **System User Token permanente** (no expira)
- [x] Registrar el número real de Sudata (`+54 9 11 2677-0450`) — status `CONNECTED`
- [x] Desactivar `META_WA_TEST_MODE` en producción
- [x] ~~Publicar la app en Meta~~ — **no es necesario**: con un número real conectado al WABA, cualquier usuario puede escribir sin necesidad de Business Verification ni modo Live (ver §5, confirmado en producción)
- [x] Configurar dominio propio con HTTPS (`reports-test.sudata.co`), reemplaza el uso de ngrok como Callback URL de producción
- [ ] Confirmar que el deploy en `reports-test.sudata.co` sea persistente (systemd/Docker con reinicio automático), no dependiente de que alguien lo levante a mano
