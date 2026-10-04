/*
 * Presentation of a ReportRun, independent of how it was obtained (polling today,
 * SSE/WebSocket, a reload, a scheduler-created run tomorrow).
 *
 *   presentRun(state)      pure view-model: what to say about the run and its questions
 *   toResultPayload(run)   adapter: ReportRun GET response -> the payload the result renderer
 *                          (tabs: informe / JSON / debug / costo) consumes
 *   createRunPanel(...)    thin DOM renderer of the view-model
 *
 * Technical strings (stages, statuses) are mapped to labels ONLY here.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReportRunView = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const TERMINAL = ['completed', 'completed_with_errors', 'failed', 'cancelled'];

  const STAGE_LABELS = Object.freeze({
    preflight: 'Preparando informe',
    analysis: 'Analizando datos',
    coordination: 'Coordinando análisis',
    writing: 'Redactando informe',
    rendering: 'Generando presentación',
  });
  const stageLabel = stage => STAGE_LABELS[stage] || null;

  // Technical section state -> visual style, kept apart from how the state is derived.
  const ITEM_STYLES = Object.freeze({
    done: {icon: '✓', cls: 'text-success', label: 'Completada'},
    failed: {icon: '✗', cls: 'text-danger', label: 'Con error'},
    running: {icon: '…', cls: 'text-primary', label: 'Generando…'},
    pending: {icon: '·', cls: 'text-muted', label: 'Pendiente'},
    skipped: {icon: '–', cls: 'text-muted', label: 'No ejecutada'},
  });

  const HEADLINES = Object.freeze({
    queued: {tone: 'info', title: 'Informe en cola', subtitle: 'Esperando ejecución…'},
    running: {tone: 'primary', title: 'Generando informe', subtitle: null},
    cancelling: {tone: 'warning', title: 'Cancelando…', subtitle: 'Esperando que la ejecución se detenga. Los análisis ya completados se conservan.'},
    completed: {tone: 'success', title: 'Informe generado', subtitle: null},
    completed_with_errors: {tone: 'warning', title: 'Informe generado con errores', subtitle: 'Algunas secciones no se pudieron completar. El informe se generó con el resto.'},
    failed: {tone: 'danger', title: 'La generación falló', subtitle: null},
    cancelled: {tone: 'secondary', title: 'Generación cancelada', subtitle: 'Los análisis completados antes de cancelar se conservan.'},
  });

  function itemsFor(run, active) {
    const done = new Map((run.sections || []).map(section => [section.key, section]));
    const planned = run.planned_sections || [];
    // Questions are started in order and at most `analysis_concurrency` at once, so the running ones are
    // exactly the first N without a result (1 for runs/payloads that predate concurrency).
    const slots = Math.max(1, Number(run.analysis_concurrency) || 1);
    let runningAssigned = 0;
    const items = planned.map(plan => {
      const section = done.get(plan.key);
      let state;
      if (section) state = section.had_error ? 'failed' : 'done';
      else if (!active) state = 'skipped';
      else if (run.current_stage === 'analysis' && runningAssigned < slots) { state = 'running'; runningAssigned += 1; }
      else state = 'pending';
      return {key: plan.key, title: plan.title, state, style: ITEM_STYLES[state], origin: 'definition'};
    });
    const plannedKeys = new Set(planned.map(plan => plan.key));
    for (const section of run.sections || []) {
      if (plannedKeys.has(section.key)) continue;
      const state = section.had_error ? 'failed' : 'done';
      items.push({key: section.key, title: section.title, state, style: ITEM_STYLES[state], origin: section.origin || 'coordinator'});
    }
    return items;
  }

  function presentRun(state) {
    const run = state && state.run;
    if (!run) return null;
    const terminal = TERMINAL.includes(run.status);
    const active = !terminal;
    let kind = run.status;
    if (active && (state.cancelling || run.status === 'cancel_requested')) kind = 'cancelling';
    const headline = HEADLINES[kind] || {tone: 'secondary', title: run.status, subtitle: null};
    const progress = run.progress || {current: 0, total: 0};
    const percent = progress.total > 0 ? Math.min(100, Math.round(progress.current / progress.total * 100)) : 0;
    const stage = stageLabel(run.current_stage);
    return {
      kind, tone: headline.tone, title: headline.title,
      subtitle: headline.subtitle || (kind === 'running' && stage ? stage : null),
      stage: active ? stage : null,
      progress: {current: progress.current, total: progress.total, percent,
                 label: `${progress.current} / ${progress.total} preguntas completadas`},
      showProgress: kind === 'running' || kind === 'cancelling' || (kind === 'queued' && progress.total > 0),
      items: itemsFor(run, active),
      canCancel: (run.status === 'queued' || run.status === 'running') && !state.cancelling,
      error: run.error ? {code: run.error.code, message: run.error.message} : null,
      connection: state.phase === 'retrying' ? {tone: 'warning', message: 'Problemas de conexión. Reintentando…'}
        : state.phase === 'stopped' && state.error ? {tone: 'danger', message: state.error.message, canRetry: ['connection_lost', 'transient'].includes(state.error.kind)}
        : null,
      cancelError: state.cancelError ? state.cancelError.message : null,
      runId: run.run_id,
      active,
    };
  }

  // The result renderer must not care where the run came from.
  // Returns null while there is nothing to show (run still active, or nothing was produced).
  function toResultPayload(run) {
    if (!run || !TERMINAL.includes(run.status)) return null;
    const result = run.result || null;
    const sections = run.sections || [];
    const cost = run.cost || null;
    if (!result && !sections.length && !(cost && cost.calls)) return null;
    return {
      report_run_id: run.run_id,
      name: run.name || 'Informe',
      markdown: (result && result.markdown) || '',
      writer: (result && result.writer) || null, // null: the run ended before writing
      final_report: (result && result.final_report) || null,
      html: (result && result.html) || null,
      coordination: (result && result.coordination) || null,
      sections,
      cost,
    };
  }

  function el(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // handlers: {onCancel, onRetry}. render(state) is idempotent and replaces the panel contents.
  function createRunPanel({root, doc, handlers}) {
    const document_ = doc || root.ownerDocument;
    function render(state) {
      const view = presentRun(state);
      root.replaceChildren();
      if (!view) { root.classList.add('d-none'); return; }
      root.classList.remove('d-none');
      const card = el(document_, 'div', `card border-${view.tone}`);
      const body = el(document_, 'div', 'card-body');
      const head = el(document_, 'div', 'd-flex flex-wrap justify-content-between align-items-start gap-2');
      const titles = el(document_, 'div');
      titles.append(el(document_, 'h4', 'h5 mb-1', view.title));
      if (view.subtitle) titles.append(el(document_, 'div', 'text-muted', view.subtitle));
      titles.append(el(document_, 'div', 'text-muted small font-monospace', `Run ID: ${view.runId}`));
      head.append(titles);
      if (view.active) {
        const cancel = el(document_, 'button', 'btn btn-outline-danger btn-sm', 'Cancelar');
        cancel.type = 'button';
        cancel.disabled = !view.canCancel;
        cancel.addEventListener('click', () => handlers.onCancel());
        head.append(cancel);
      }
      body.append(head);
      if (view.showProgress) {
        const bar = el(document_, 'div', 'progress my-3');
        const fill = el(document_, 'div', 'progress-bar progress-bar-striped progress-bar-animated');
        fill.style.width = `${view.progress.percent}%`;
        fill.setAttribute('role', 'progressbar');
        bar.append(fill);
        body.append(bar, el(document_, 'div', 'small', view.progress.label));
      }
      if (view.error) body.append(el(document_, 'div', 'alert alert-danger mt-3 mb-0', view.error.message || 'La ejecución falló.'));
      if (view.cancelError) body.append(el(document_, 'div', 'alert alert-warning mt-3 mb-0', view.cancelError));
      if (view.connection) {
        const box = el(document_, 'div', `alert alert-${view.connection.tone} mt-3 mb-0 d-flex justify-content-between align-items-center gap-2`);
        box.append(el(document_, 'span', null, view.connection.message));
        if (view.connection.canRetry) {
          const retry = el(document_, 'button', 'btn btn-sm btn-outline-secondary', 'Reintentar');
          retry.type = 'button';
          retry.addEventListener('click', () => handlers.onRetry());
          box.append(retry);
        }
        body.append(box);
      }
      if (view.items.length) {
        const list = el(document_, 'ul', 'list-unstyled mt-3 mb-0 small');
        for (const item of view.items) {
          const row = el(document_, 'li', `d-flex justify-content-between gap-3 py-1 border-bottom ${item.style.cls}`);
          row.append(el(document_, 'span', null, `${item.style.icon} ${item.title}`), el(document_, 'span', null, item.style.label));
          list.append(row);
        }
        body.append(list);
      }
      card.append(body);
      root.append(card);
    }
    return {render};
  }

  return {STAGE_LABELS, ITEM_STYLES, stageLabel, presentRun, toResultPayload, createRunPanel};
});
