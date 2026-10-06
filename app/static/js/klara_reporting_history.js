(function () {
  'use strict';
  const cfg = window.KLARA_HISTORY;
  const body = document.getElementById('history-body');
  const message = document.getElementById('history-message');
  const summary = document.getElementById('history-summary');
  const prev = document.getElementById('history-prev');
  const next = document.getElementById('history-next');
  const form = document.getElementById('history-filters');
  const POLL_MS = 5000;
  const BADGES = {
    completed: 'success', completed_with_errors: 'warning', failed: 'danger',
    cancelled: 'secondary', queued: 'info', running: 'primary', cancel_requested: 'warning',
  };
  let page = 1, pages = 1, timer = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatDate(iso) {
    if (!iso) return '—';
    const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
    return date.toLocaleString();
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    const m = Math.floor(seconds / 60), s = seconds % 60;
    return m ? m + ' min ' + s + ' s' : s + ' s';
  }

  function showError(text) {
    message.textContent = text || '';
    message.classList.toggle('d-none', !text);
  }

  function row(item) {
    const tr = document.createElement('tr');
    tr.appendChild(el('td', 'fw-semibold', item.name || 'Informe'));
    tr.appendChild(el('td', '', item.report_name || '—'));
    const status = el('td');
    const badge = el('span', 'badge text-bg-' + (BADGES[item.status] || 'secondary'), item.status);
    status.appendChild(badge);
    if (!item.is_terminal && item.progress && item.progress.total) {
      status.appendChild(el('small', 'text-muted ms-2', item.progress.current + '/' + item.progress.total));
    }
    if (item.error_message) badge.title = item.error_message;
    tr.appendChild(status);
    tr.appendChild(el('td', '', item.requested_by || '—'));
    tr.appendChild(el('td', '', formatDate(item.created_at)));
    tr.appendChild(el('td', '', formatDuration(item.duration_seconds)));
    const actions = el('td', 'text-end');
    const open = el('a', 'btn btn-sm btn-outline-primary', 'Abrir');
    open.href = cfg.runUrl + '?run_id=' + encodeURIComponent(item.run_id);
    actions.appendChild(open);
    if (!item.is_terminal && item.status !== 'cancel_requested') {
      const cancel = el('button', 'btn btn-sm btn-outline-danger ms-1', 'Cancelar');
      cancel.type = 'button';
      cancel.addEventListener('click', () => cancelRun(item.run_id, cancel));
      actions.appendChild(cancel);
    }
    tr.appendChild(actions);
    return tr;
  }

  function query() {
    const params = new URLSearchParams(new FormData(form));
    for (const [key, value] of [...params]) if (!value) params.delete(key);
    params.set('page', page);
    return params.toString();
  }

  async function load() {
    clearTimeout(timer);
    try {
      const response = await fetch(cfg.endpoint + '?' + query(), {credentials: 'same-origin'});
      if (!response.ok) throw new Error('No se pudo cargar el historial.');
      const data = await response.json();
      showError('');
      pages = data.pages;
      body.replaceChildren(...(data.items.length
        ? data.items.map(row)
        : [(() => { const tr = document.createElement('tr'); const td = el('td', 'text-center text-muted py-4', 'No hay ejecuciones.'); td.colSpan = 7; tr.appendChild(td); return tr; })()]));
      summary.textContent = data.total + ' ejecuciones · página ' + data.page + ' de ' + data.pages;
      prev.disabled = data.page <= 1;
      next.disabled = data.page >= data.pages;
      // Poll softly only while something on this page is still in flight.
      if (data.items.some((item) => !item.is_terminal)) timer = setTimeout(load, POLL_MS);
    } catch (error) {
      showError(error.message || 'No se pudo cargar el historial.');
    }
  }

  async function cancelRun(runId, button) {
    button.disabled = true;
    try {
      const response = await fetch(cfg.cancelUrl.replace('{id}', encodeURIComponent(runId)), {
        method: 'POST', credentials: 'same-origin', headers: {'X-CSRFToken': cfg.csrf},
      });
      if (!response.ok) throw new Error('No se pudo cancelar la ejecución.');
      showError('');
    } catch (error) {
      showError(error.message);
    }
    load();
  }

  form.addEventListener('submit', (event) => { event.preventDefault(); page = 1; load(); });
  form.addEventListener('reset', () => setTimeout(() => { page = 1; load(); }));
  prev.addEventListener('click', () => { if (page > 1) { page -= 1; load(); } });
  next.addEventListener('click', () => { if (page < pages) { page += 1; load(); } });
  load();
})();
