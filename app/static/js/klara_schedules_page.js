/*
 * DOM controller of the scheduled-reports screen: list, create/edit form with live preview, occurrence history.
 *
 * Wiring only: texts and shapes come from KlaraSchedulesView, HTTP from KlaraSchedulesApi. Every value from the
 * server is written with textContent (never innerHTML). The server decides what is possible (`can_retry`); the
 * page only asks for confirmation before actions that may spend AI tokens (run now, retrying a failed run).
 */
(function (root) {
  'use strict';

  const View = root.KlaraSchedulesView;
  const Api = root.KlaraSchedulesApi;

  function el(doc, tag, attrs, children) {
    const node = doc.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (key === 'text') node.textContent = value;
      else if (key === 'className') node.className = value;
      else node.setAttribute(key, value === true ? '' : value);
    });
    (children || []).forEach(child => { if (child) node.appendChild(child); });
    return node;
  }

  function badge(doc, text, tone) {
    return el(doc, 'span', {className: 'ai-pill tone-' + (tone || 'neutral'), text});
  }

  function mount({config, csrf, doc}) {
    const api = Api.createSchedulesApi({endpoints: config.endpoints, csrf});
    const $ = id => doc.getElementById(id);
    const state = {schedules: [], editingId: null, historyId: null, historyPage: 1, previewTimer: null};
    const urls = {runUrl: config.runUrl, reviewUrl: config.reviewUrl};

    function message(text, tone) {
      const box = $('rs-message');
      box.textContent = text || '';
      box.className = 'alert alert-' + (tone || 'danger') + (text ? '' : ' d-none');
    }

    function fail(error) {
      message(error && error.message ? error.message : 'Error inesperado.', 'danger');
    }

    // ── form ─────────────────────────────────────────────────────────────────────────────────

    function fillSelect(select, options, selected) {
      select.textContent = '';
      options.forEach(([value, label]) => {
        const option = el(doc, 'option', {value, text: label});
        if (String(value) === String(selected)) option.selected = true;
        select.appendChild(option);
      });
    }

    function setupForm() {
      fillSelect($('rs-definition'), [['', 'Elegí una definición…']].concat(
        config.definitions.map(d => [d.id, d.name + (d.report_name ? ' — ' + d.report_name : '')])), '');
      fillSelect($('rs-frequency'), config.frequencies.map(f => [f, (View.FREQUENCIES[f] || {label: f}).label]), 'weekly');
      fillSelect($('rs-weekday'), View.WEEKDAYS.map((name, index) => [index, name]), 0);
      fillSelect($('rs-freshness'), config.freshnessPolicies.map(p => [p, (View.FRESHNESS_POLICIES[p] || {label: p}).label]),
        config.defaultFreshnessPolicy);
      fillSelect($('rs-misfire'), config.misfirePolicies.map(p => [p, (View.MISFIRE_POLICIES[p] || {label: p}).label]),
        config.defaultMisfirePolicy);
      const list = $('rs-timezones');
      config.timezones.forEach(zone => list.appendChild(el(doc, 'option', {value: zone})));
      $('rs-form').addEventListener('submit', event => { event.preventDefault(); save(); });
      $('rs-form').addEventListener('input', schedulePreview);
      $('rs-frequency').addEventListener('change', toggleFrequency);
      $('rs-cancel').addEventListener('click', closeForm);
      $('rs-new').addEventListener('click', () => openForm(null));
    }

    function formValues() {
      const value = id => $(id).value;
      const values = {
        name: value('rs-name'), frequency: value('rs-frequency'), weekday: value('rs-weekday'),
        day_of_month: value('rs-day'), local_time: value('rs-time'), timezone: value('rs-timezone'),
        freshness_policy: value('rs-freshness'), misfire_policy: value('rs-misfire'),
        refresh_deadline_hours: value('rs-deadline'), misfire_grace_hours: value('rs-grace'),
        whatsapp_recipients: value('rs-whatsapp'),
      };
      if (state.editingId === null) values.definition_id = value('rs-definition');
      return values;
    }

    function toggleFrequency() {
      const weekly = $('rs-frequency').value === 'weekly';
      $('rs-weekday-group').classList.toggle('d-none', !weekly);
      $('rs-day-group').classList.toggle('d-none', weekly);
      $('rs-period-hint').textContent = (View.FREQUENCIES[$('rs-frequency').value] || {}).hint || '';
    }

    function openForm(schedule) {
      state.editingId = schedule ? schedule.id : null;
      const values = schedule ? View.scheduleToForm(schedule) : {
        frequency: 'weekly', weekday: '0', day_of_month: '1', local_time: '02:00', timezone: config.defaultTimezone,
        freshness_policy: config.defaultFreshnessPolicy, misfire_policy: config.defaultMisfirePolicy,
      };
      [['rs-name', 'name'], ['rs-frequency', 'frequency'], ['rs-weekday', 'weekday'], ['rs-day', 'day_of_month'],
        ['rs-time', 'local_time'], ['rs-timezone', 'timezone'], ['rs-freshness', 'freshness_policy'],
        ['rs-misfire', 'misfire_policy'], ['rs-deadline', 'refresh_deadline_hours'], ['rs-grace', 'misfire_grace_hours'],
        ['rs-whatsapp', 'whatsapp_recipients'],
      ].forEach(([id, key]) => { $(id).value = values[key] || (id === 'rs-day' ? '1' : ''); });
      $('rs-definition').value = values.definition_id || '';
      $('rs-definition').disabled = !!schedule;  // a schedule never changes definition: create another one
      $('rs-form-title').textContent = schedule ? 'Editar programación' : 'Nueva programación';
      $('rs-form-card').classList.remove('d-none');
      toggleFrequency();
      schedulePreview();
    }

    function closeForm() {
      state.editingId = null;
      $('rs-form-card').classList.add('d-none');
    }

    function schedulePreview() {
      if (state.previewTimer) clearTimeout(state.previewTimer);
      state.previewTimer = setTimeout(renderPreview, 400);
    }

    async function renderPreview() {
      const list = $('rs-preview');
      list.textContent = '';
      try {
        // The preview only depends on the calendar: a half-typed number must not break it.
        const data = await api.preview(Object.assign(View.formToPayload(formValues()), {whatsapp_recipients: []}));
        data.items.forEach(item => {
          const view = View.presentPreview(item);
          list.appendChild(el(doc, 'li', {className: 'small'}, [
            el(doc, 'strong', {text: view.when}), doc.createTextNode(' → ' + view.period + (view.comparison ? ' · ' + view.comparison : '')),
          ]));
        });
      } catch (error) {
        list.appendChild(el(doc, 'li', {className: 'small text-muted', text: error.message}));
      }
    }

    async function save() {
      const payload = View.formToPayload(formValues());
      try {
        const saved = state.editingId === null ? await api.create(payload) : await api.update(state.editingId, payload);
        closeForm();
        message('Programación guardada · próxima ejecución: ' + (saved.is_active
          ? View.formatLocalDateTime(saved.next_run_local) + ' (' + saved.timezone + ')' : 'pausada'), 'success');
        await loadList();
        await showHistory(saved.id);
      } catch (error) { fail(error); }
    }

    // ── list ─────────────────────────────────────────────────────────────────────────────────

    function actionButton(label, icon, handler, extraClass) {
      const button = el(doc, 'button', {type: 'button', className: (extraClass || 'ai-btn-ghost') + ' btn-sm', title: label}, [
        el(doc, 'i', {className: 'bi bi-' + icon})]);
      button.addEventListener('click', handler);
      return button;
    }

    function renderHealth(health) {
      const box = $('rs-scheduler-health');
      if (!box) return;
      const view = View.presentSchedulerHealth(health);
      box.textContent = view ? view.text : '';
      box.className = 'alert alert-' + (view ? view.tone : 'secondary') + (view ? '' : ' d-none');
    }

    async function loadList() {
      const body = $('rs-body');
      try {
        const data = await api.list();
        state.schedules = data.items;
        renderHealth(data.scheduler);
      } catch (error) { fail(error); return; }
      body.textContent = '';
      if (!state.schedules.length) {
        body.appendChild(el(doc, 'tr', {}, [el(doc, 'td', {colspan: '6', className: 'text-center text-muted py-4',
          text: 'Todavía no hay informes programados.'})]));
        return;
      }
      state.schedules.forEach(schedule => {
        const view = View.presentSchedule(schedule);
        const actions = el(doc, 'td', {className: 'text-end text-nowrap'}, [
          actionButton('Historial', 'clock-history', () => showHistory(schedule.id)),
          actionButton('Editar', 'pencil', () => openForm(schedule)),
          view.active ? actionButton('Pausar', 'pause-circle', () => toggle(schedule, false))
            : actionButton('Reanudar', 'play-circle', () => toggle(schedule, true)),
          actionButton('Ejecutar ahora', 'lightning', () => runNow(schedule)),
        ]);
        body.appendChild(el(doc, 'tr', {}, [
          el(doc, 'td', {}, [el(doc, 'div', {className: 'fw-semibold', text: view.name}),
            el(doc, 'small', {className: 'text-muted', text: view.definition})]),
          el(doc, 'td', {text: view.report}),
          el(doc, 'td', {text: view.calendar}),
          el(doc, 'td', {text: view.nextRun}),
          el(doc, 'td', {}, [badge(doc, view.stateLabel, view.stateTone)]),
          actions,
        ]));
      });
    }

    async function toggle(schedule, active) {
      try {
        await (active ? api.resume(schedule.id) : api.pause(schedule.id));
        message(active ? 'Programación reanudada: no se ejecutan los horarios perdidos mientras estuvo pausada.'
          : 'Programación pausada.', 'success');
        await loadList();
      } catch (error) { fail(error); }
    }

    async function runNow(schedule) {
      if (!root.confirm('Se va a crear una ejecución manual de «' + schedule.name + '» ahora, con el período que le '
        + 'corresponde. Espera los datos de Power BI y consume IA. ¿Continuar?')) return;
      try {
        await api.runNow(schedule.id);
        message('Ejecución manual creada: espera datos y se encola sola.', 'success');
        await showHistory(schedule.id);
      } catch (error) { fail(error); }
    }

    // ── history ──────────────────────────────────────────────────────────────────────────────

    async function showHistory(scheduleId, page) {
      state.historyId = scheduleId;
      state.historyPage = page || 1;
      const schedule = state.schedules.find(item => item.id === scheduleId);
      $('rs-history-card').classList.remove('d-none');
      $('rs-history-title').textContent = 'Historial — ' + (schedule ? schedule.name : '#' + scheduleId);
      let data;
      try {
        data = await api.occurrences(scheduleId, state.historyPage);
      } catch (error) { fail(error); return; }
      const counts = $('rs-counts');
      counts.textContent = '';
      View.presentCounts(data.status_counts).forEach(item => {
        counts.appendChild(badge(doc, item.label + ': ' + item.count, item.tone));
        counts.appendChild(doc.createTextNode(' '));
      });
      const body = $('rs-history-body');
      body.textContent = '';
      if (!data.items.length) {
        body.appendChild(el(doc, 'tr', {}, [el(doc, 'td', {colspan: '7', className: 'text-center text-muted py-4',
          text: 'Sin ocurrencias todavía.'})]));
      }
      data.items.forEach(item => body.appendChild(historyRow(View.presentOccurrence(item, urls))));
      $('rs-history-summary').textContent = 'Página ' + data.page + ' de ' + data.pages + ' · ' + data.total + ' ocurrencias';
      $('rs-prev').disabled = data.page <= 1;
      $('rs-next').disabled = data.page >= data.pages;
    }

    function historyRow(view) {
      const runCell = el(doc, 'td');
      if (view.run) {
        runCell.appendChild(view.run.url ? el(doc, 'a', {href: view.run.url, text: view.run.status}) : el(doc, 'span', {text: view.run.status}));
        runCell.appendChild(el(doc, 'div', {className: 'small text-muted',
          text: view.run.duration + ' · ' + view.run.cost + (view.run.attempts > 1 ? ' · ' + view.run.attempts + ' intentos' : '')}));
      } else {
        runCell.textContent = '—';
      }
      const reviewCell = el(doc, 'td');
      if (view.review) reviewCell.appendChild(el(doc, 'a', {href: view.review.url, text: view.review.status}));
      else reviewCell.textContent = '—';
      const actions = el(doc, 'td', {className: 'text-end'});
      if (view.canRetry) actions.appendChild(actionButton('Reintentar', 'arrow-repeat', () => retry(view)));
      return el(doc, 'tr', {}, [
        el(doc, 'td', {}, [el(doc, 'div', {text: view.scheduled}),
          view.manual ? el(doc, 'small', {className: 'text-muted', text: 'manual'}) : null]),
        el(doc, 'td', {text: view.period}),
        el(doc, 'td', {}, [badge(doc, view.status.label, view.status.tone),
          view.error ? el(doc, 'div', {className: 'small text-muted', text: view.error}) : null]),
        el(doc, 'td', {className: 'small', text: view.refresh}),
        runCell, reviewCell, actions,
      ]);
    }

    async function retry(view) {
      const question = view.retryNeedsConfirm
        ? 'La ejecución anterior falló. Reintentar crea una NUEVA ejecución y vuelve a consumir IA. ¿Continuar?'
        : '¿Volver a esperar los datos de esta ocurrencia con un nuevo plazo?';
      if (!root.confirm(question)) return;
      try {
        await api.retry(view.id);
        message('Ocurrencia reabierta.', 'success');
        await showHistory(state.historyId, state.historyPage);
      } catch (error) { fail(error); }
    }

    $('rs-prev').addEventListener('click', () => showHistory(state.historyId, state.historyPage - 1));
    $('rs-next').addEventListener('click', () => showHistory(state.historyId, state.historyPage + 1));
    $('rs-refresh').addEventListener('click', async () => {
      message('');
      await loadList();
      if (state.historyId !== null) await showHistory(state.historyId, state.historyPage);
    });
    setupForm();
    loadList();
  }

  root.KlaraSchedulesPage = {mount};
})(typeof self !== 'undefined' ? self : this);
