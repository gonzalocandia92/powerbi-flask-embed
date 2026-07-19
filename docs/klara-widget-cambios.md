# KLARA — Widget y cambios realizados

Resumen de lo que se implementó/modificó en el proyecto para el chatbot web.

Archivo principal: `app/templates/report_base.html`

---

## 1. Toggles de visibilidad de KLARA

### `chatbot_enabled` (por reporte)
Campo en la tabla `reports`. Cuando está en `false`, el bloque completo del widget no se renderiza — no aparece ni el botón K ni el panel lateral.

En el template, todo el widget está envuelto en:
```html
{% if chatbot_enabled %}
  <!-- widget completo -->
{% endif %}
```

Se configura desde **Admin → Reportes → Editar → KLARA habilitado**.

### `show_dax_query` (por reporte)
Campo en la tabla `reports`. Cuando está en `true`, debajo de cada respuesta del bot aparece un bloque gris azulado con la consulta DAX que se ejecutó.

Se pasa al template como variable y se expone al JS:
```js
var KLARA_SHOW_DAX = {{ show_dax_query | default(false) | tojson }};
```

En la función `addMessage()`, si `sqlUsado` viene en la respuesta y `KLARA_SHOW_DAX` está activo, se agrega el bloque de debug:
```js
if (sqlUsado && KLARA_SHOW_DAX) {
    var dbg = document.createElement("div");
    dbg.className = "klara-sql-debug";
    dbg.textContent = "DAX ejecutado: " + sqlUsado;
    bubble.appendChild(dbg);
}
```

CSS del bloque DAX:
```css
.klara-sql-debug {
    margin-top: 8px; padding: 8px; border-radius: 10px;
    background: #eef2ff; color: #3730a3; font-family: monospace;
    font-size: 11px; line-height: 1.4; overflow-x: auto;
    border: 1px solid #c7d2fe;
}
```

---

## 2. Renderizado Markdown en el chat

Las respuestas del bot se renderizan con **marked.js** (cargado desde CDN):
```html
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
```

En la función `addMessage()`, los mensajes del bot pasan por `marked.parse()`:
```js
bubble.innerHTML = type === 'bot'
    ? marked.parse(text || "")
    : sanitize(text).replace(/\n/g, "<br>");
```

Los mensajes del usuario se sanitizan con `textContent` para evitar XSS — no se renderiza markdown en ellos.

### Elementos markdown con estilo propio dentro del bubble del bot

```css
.klara-msg.bot .klara-bubble p           { margin: 0 0 6px 0; }
.klara-msg.bot .klara-bubble h1-h4       { margin: 8px 0 4px; font-size: 1em; font-weight: 700; }
.klara-msg.bot .klara-bubble ul, ol      { margin: 4px 0 6px; padding-left: 18px; }
.klara-msg.bot .klara-bubble code        { background: #f1f5f9; border-radius: 4px; padding: 1px 5px; font-family: monospace; font-size: 12px; }
.klara-msg.bot .klara-bubble pre         { background: #f1f5f9; border-radius: 8px; padding: 10px; overflow-x: auto; }
.klara-msg.bot .klara-bubble blockquote  { border-left: 3px solid var(--sudata-green); margin: 6px 0; padding-left: 10px; color: #555; }
.klara-msg.bot .klara-bubble strong      { font-weight: 700; }
.klara-msg.bot .klara-bubble em          { font-style: italic; }
.klara-msg.bot .klara-bubble hr          { border: none; border-top: 1px solid var(--sudata-border); margin: 8px 0; }
```

---

## 3. Formato de tablas dentro del chat

**Problema:** `marked.parse()` convierte las tablas markdown (`| col | col |`) a HTML `<table>` correctamente, pero sin CSS las celdas no tienen bordes ni separación visual.

**Solución:** Se agregaron estilos específicos para `table`, `th`, `td`, `thead` y `tbody` dentro de `.klara-msg.bot .klara-bubble`:

```css
.klara-msg.bot .klara-bubble table {
    border-collapse: collapse;
    width: 100%;
    margin: 8px 0;
    font-size: 13px;
    display: block;
    overflow-x: auto;      /* tablas anchas scrollean dentro del bubble */
}
.klara-msg.bot .klara-bubble th,
.klara-msg.bot .klara-bubble td {
    border: 1px solid #d1d5db;
    padding: 6px 10px;
    text-align: left;
    white-space: nowrap;
}
.klara-msg.bot .klara-bubble thead tr {
    background: #f0fdf4;   /* verde muy claro */
    color: #166534;
    font-weight: 700;
}
.klara-msg.bot .klara-bubble tbody tr:nth-child(even) {
    background: #f9fafb;   /* gris alternado */
}
.klara-msg.bot .klara-bubble tbody tr:hover {
    background: #f0fdf4;
}
```

`display: block` + `overflow-x: auto` en `table` es clave: permite que tablas con muchas columnas scrolleen horizontalmente dentro del bubble sin romper el layout de la página.

---

## 4. Widget KLARA sidebar

El widget completo vive en `report_base.html` dentro del bloque `{% if chatbot_enabled %}`.

### Estructura HTML
```
#klara-sidebar-widget
├── #klara-sidebar-button     ← botón flotante circular (K)
└── #klara-sidebar-panel      ← panel lateral deslizable
    ├── .klara-sidebar-header     ← header con título + botones expandir/cerrar
    ├── .klara-context            ← "Contexto activo / Dashboard activo"
    ├── #klara-sidebar-messages   ← área de mensajes
    ├── .klara-sidebar-suggestions ← 3 sugerencias predefinidas
    └── .klara-sidebar-input      ← form con textarea + botón enviar
```

### Comportamiento del panel
- **Ancho normal:** `--klara-sidebar-w: 420px`
- **Expandido:** `--klara-sidebar-expanded-w: 760px` (botón ⛶)
- **Apertura/cierre:** `transform: translateX(100%)` → `translateX(0)` con transición CSS de 300ms
- **Desplaza el tablero:** cuando el panel se abre, `body` recibe la clase `klara-sidebar-open` que agrega `padding-right` igual al ancho del panel, empujando el contenido hacia la izquierda. En fullscreen, ajusta el `panViewport` por la derecha.

### Mensajes
- **Usuario:** bubble verde, texto plano, alineado a la derecha
- **Bot:** bubble blanco con borde, markdown renderizado, alineado a la izquierda
- **Timestamp:** debajo de cada bubble en texto pequeño gris
- **Separador de fecha:** línea horizontal con el día (hoy / ayer / lunes / dd/mm/yyyy)

### Animación de espera — 3 puntos + mensajes de progreso

Mientras el agente procesa la consulta, se muestra primero una animación de 3 puntos pulsantes y luego mensajes de texto itálico que van reemplazándola progresivamente.

**Fase 1 — 3 puntos (`.klara-typing`)**

Aparece inmediatamente al enviar el mensaje. Son 3 círculos verdes que suben y bajan en cascada:

```css
.klara-typing {
    display: flex; gap: 5px;
    padding: 12px 14px; border-radius: 16px; border-bottom-left-radius: 5px;
    background: white; border: 1px solid var(--sudata-border);
}
.klara-typing span {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--sudata-green);
    animation: klaraTyping 1.2s infinite ease-in-out;
}
.klara-typing span:nth-child(2) { animation-delay: .16s; }
.klara-typing span:nth-child(3) { animation-delay: .32s; }

@keyframes klaraTyping {
    0%, 80%, 100% { opacity: .35; transform: translateY(0); }
    40%           { opacity: 1;   transform: translateY(-3px); }
}
```

**Fase 2 — Mensajes de progreso (`.klara-thinking-text`)**

A partir de los 5 segundos, el elemento `.klara-typing` cambia su clase a `.klara-thinking-text` y muestra un texto itálico en gris. El texto se actualiza cada 5 segundos según esta secuencia:

```js
var thinkingSteps = [
    { delay:  5000, text: "Estoy analizando los datos..." },
    { delay: 10000, text: "Buscando las métricas relevantes..." },
    { delay: 15000, text: "Ahora estoy preparando la respuesta..." },
    { delay: 20000, text: "Ordenando la información..." },
    { delay: 25000, text: "Dándole los últimos detalles..." },
    { delay: 30000, text: "Aplicando el formato final..." },
    { delay: 35000, text: "Ya casi está..." }
];
```

```css
.klara-thinking-text {
    padding: 9px 14px; border-radius: 16px; border-bottom-left-radius: 5px;
    background: white; border: 1px solid var(--sudata-border);
    font-size: 13px; color: var(--sudata-muted); font-style: italic;
    animation: klaraMsgIn .22s ease;
}
```

**Ciclo completo:**

```
Envío → [puntos animados]
→ 5s  → "Estoy analizando los datos..."
→ 10s → "Buscando las métricas relevantes..."
→ 15s → "Ahora estoy preparando la respuesta..."
→ 20s → "Ordenando la información..."
→ 25s → "Dándole los últimos detalles..."
→ 30s → "Aplicando el formato final..."
→ 35s → "Ya casi está..."
→ respuesta llega → todo desaparece, se muestra el bubble del bot
```

Cuando llega la respuesta, `hideThinking()` cancela todos los timers pendientes con `clearTimeout` y elimina el elemento del DOM. El botón de envío vuelve a habilitarse.

### Envío de mensajes
```js
fetch("/chat", {
    method: "POST",
    body: JSON.stringify({
        message: pregunta,
        slug: KLARA_SLUG,           // slug del public link activo
        conversation_id: klaraSessionId  // null en la primera consulta
    })
})
```
La respuesta incluye `answer`, `conversation_id` (para mantener el hilo) y opcionalmente `dax_query`.

### Variables JS expuestas
```js
var KLARA_SLUG = "{{ slug | default('') }}";       // slug del reporte activo
var KLARA_SHOW_DAX = {{ show_dax_query | tojson }}; // mostrar consulta DAX
```
