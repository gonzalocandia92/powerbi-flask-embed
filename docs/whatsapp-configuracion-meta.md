# Configuración WhatsApp — Lado Meta

Todo lo que hay que hacer en Meta para que el chatbot funcione.

---

## Qué necesitás antes de empezar

- Una cuenta de Facebook personal con acceso al Meta Business Account de la empresa
- El número de WhatsApp Business ya conectado al WABA (WhatsApp Business Account)

---

## Paso 1 — Crear cuenta de desarrollador

1. Entrá a **developers.facebook.com**
2. Iniciá sesión con tu cuenta de Facebook personal
3. Aceptá las políticas de desarrollador si es la primera vez

---

## Paso 2 — Crear la App

1. En el dashboard, clic en **"Create App"**
2. Tipo de app: **Business**
3. Nombre: algo como `klara-whatsapp` (es solo interno, no lo ve nadie)
4. Vinculá la app a tu **Meta Business Account**
5. Clic en **"Create App"**

---

## Paso 3 — Agregar el producto WhatsApp

1. Dentro de la app, buscá el producto **WhatsApp** en la lista y hacé clic en **"Set up"**
2. Vinculá al **WhatsApp Business Account (WABA)** donde está el número

---

## Paso 4 — Obtener las credenciales

En el panel de **WhatsApp → API Setup** vas a encontrar:

| Dato | Dónde está | Variable de entorno |
|------|-----------|---------------------|
| **Phone Number ID** | Sección "From" del panel | `META_WA_PHONE_NUMBER_ID` |
| **Access Token temporal** | Sección "Access Token" (dura 24hs) | `META_WA_ACCESS_TOKEN` |
| **App Secret** | Settings → Basic → App Secret | `META_WA_APP_SECRET` |

El **Verify Token** (`META_WA_VERIFY_TOKEN`) lo elegís vos libremente — cualquier string, ej: `klara2026`.

> Para producción reemplazá el token temporal por un **System User Token permanente** (no vence). Se genera en Business Settings → System Users.

---

## Paso 5 — Configurar el Webhook

El webhook es la URL donde Meta va a enviar cada mensaje que reciba el número.

1. En el panel de WhatsApp, ir a **"Configuration" → "Webhook"**
2. Clic en **"Edit"**
3. Completar:
   - **Callback URL:** `https://TU-DOMINIO/webhook/whatsapp`
   - **Verify Token:** el valor de `META_WA_VERIFY_TOKEN`
4. Clic en **"Verify and Save"** — Meta va a hacer un GET a esa URL para verificarla, el servidor tiene que estar corriendo
5. En **"Webhook fields"**, suscribir el campo **`messages`**

### Para pruebas locales (ngrok)

Si estás en local, la Callback URL tiene que ser pública. Usá ngrok:

```powershell
# En una terminal aparte, con la app corriendo en puerto 2052
ngrok http 2052
```

Ngrok te da una URL tipo `https://xxxx.ngrok-free.app` — esa es la que ponés en Meta como Callback URL.

> Con el plan gratuito de ngrok la URL cambia cada vez que lo reiniciás. Con un dominio reservado (plan pago) queda fija.

---

## Paso 6 — Suscribir la app al WABA

Esto se hace una sola vez. Permite que tu app reciba los eventos del número:

```bash
curl -X POST "https://graph.facebook.com/v20.0/<WABA_ID>/subscribed_apps" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

El **WABA_ID** lo encontrás en WhatsApp → API Setup, arriba del Phone Number ID.

---

## Paso 7 — Agregar testers (solo si usás el número de prueba gratuito de Meta)

Si estás usando el número de sandbox que Meta da por defecto (no tu número real), solo pueden enviarte mensajes los números que agregues como testers:

1. WhatsApp → API Setup → **"Send and receive messages"**
2. Sección **"To"** → **"Manage phone number list"**
3. **"Add recipient phone number"** → agregá tu número personal

> Si usás un número real verificado (`CONNECTED`) esto **no aplica** — cualquier usuario puede escribirle.

---

## Variables de entorno necesarias

```env
META_WA_PHONE_NUMBER_ID=123456789012345
META_WA_ACCESS_TOKEN=EAABxx...
META_WA_VERIFY_TOKEN=klara2026
META_WA_APP_SECRET=abc123...
META_WA_TEST_MODE=true   # true en local, false en producción
```

---

## Checklist de verificación

- [ ] App creada en Meta Developer Console
- [ ] Producto WhatsApp agregado y vinculado al WABA
- [ ] Phone Number ID copiado al `.env`
- [ ] Access Token copiado al `.env`
- [ ] App Secret copiado al `.env`
- [ ] Webhook configurado con la URL correcta y campo `messages` suscripto
- [ ] App suscripta al WABA (curl del paso 6)
- [ ] ngrok corriendo (solo para local)

---

## Estado actual de la app de Meta en producción

Esta sección describe la app de Meta que está activa hoy para el chatbot de WhatsApp de Sudata.

### Número

`+54 9 11 2677-0450` — status **CONNECTED**

Es un número real verificado, conectado al WABA de Sudata. Al ser un número CONNECTED (no el sandbox de Meta), cualquier usuario de WhatsApp puede escribirle — no hay restricción de "testers" ni lista de destinatarios permitidos.

### Token de acceso

Se usa un **System User Token permanente** (no el token temporal de 24 horas que Meta muestra en el panel por defecto). El token permanente se genera desde **Business Settings → System Users** y no vence.

Variable: `META_WA_ACCESS_TOKEN` en el `.env` del servidor.

### Webhook en producción

La Callback URL configurada en Meta Developers apunta al dominio propio:

```
https://reports-test.sudata.co/webhook/whatsapp
```

Ngrok ya no se usa en producción — solo para desarrollo local.

### Suscripción al WABA

Ya está hecha (se ejecutó el curl del Paso 6 al momento del setup inicial). No hace falta repetirla salvo que se cambie la app o el WABA.

### `META_WA_TEST_MODE`

En producción: `META_WA_TEST_MODE=false`

Con `false`, no se activa la normalización de números argentinos (eso solo era necesario en desarrollo, donde Meta entregaba los números en el formato legacy `54XXX15XXXXXXX` en lugar del moderno `549XXXXXXXXX`).

### ¿Hay que publicar la app?

**No.** Con un número real conectado al WABA propio, la app puede operar en modo Development sin necesidad de Business Verification ni de pasarla a modo Live. El flujo (mensajes a clientes con el número de Sudata) no requiere permisos avanzados de Graph API que exijan publicación. Confirmado en producción: usuarios autorizados recibieron respuesta sin que la app estuviera en modo Live.
