/*
 * Presentation model of the report-review screens (inbox and detail). Pure: no DOM, no HTTP.
 *
 * Everything a person reads (labels, tones, texts) lives in the DATA TABLES below, keyed by the stable values
 * the server sends (status, event type, error code, action name). Rendering code only looks values up, so:
 *   - a new status / event type / error code is one more row (and works meanwhile: unknown values fall back to
 *     a neutral presentation that shows the raw value and the server's own message);
 *   - the UI never decides what a person may do: buttons come from the `actions` list the server returns
 *     (`available_actions`), each one needing a row in ACTIONS to know how to look and behave.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReviewView = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ── data tables ────────────────────────────────────────────────────────────────────────────

  // Review status -> look. Keys must cover persistence/review_states.STATUSES (a Python test enforces it).
  const REVIEW_STATUSES = Object.freeze({
    draft: {label: 'Borrador', tone: 'neutral', icon: 'pencil-square'},
    pending_review: {label: 'Pendiente de revisión', tone: 'warning', icon: 'hourglass-split'},
    approved: {label: 'Aprobado', tone: 'success', icon: 'check-circle'},
    rejected: {label: 'Rechazado', tone: 'danger', icon: 'x-circle'},
  });

  // Technical status of the generation run (NOT the human decision).
  const RUN_STATUSES = Object.freeze({
    completed: {label: 'Completada', tone: 'success'},
    completed_with_errors: {label: 'Completada con errores', tone: 'warning'},
    failed: {label: 'Fallida', tone: 'danger'},
    cancelled: {label: 'Cancelada', tone: 'neutral'},
    queued: {label: 'En cola', tone: 'info'},
    running: {label: 'En ejecución', tone: 'primary'},
    cancel_requested: {label: 'Cancelando', tone: 'warning'},
  });

  // Audit event type -> look. Covers persistence/review_states.EVENT_TYPES (a Python test enforces it).
  const EVENT_TYPES = Object.freeze({
    submitted: {label: 'Enviado a revisión', tone: 'primary', icon: 'send'},
    commented: {label: 'Observación', tone: 'info', icon: 'chat-left-text'},
    approved: {label: 'Aprobado', tone: 'success', icon: 'check-circle'},
    rejected: {label: 'Rechazado', tone: 'danger', icon: 'x-circle'},
  });

  // Why a run cannot be reviewed (eligibility code). The server's message is the fallback.
  const ELIGIBILITY_REASONS = Object.freeze({
    run_not_found: 'La ejecución no existe.',
    run_not_completed: 'La ejecución todavía no terminó.',
    run_completed_with_errors: 'La ejecución terminó con errores: no puede aprobarse.',
    final_report_missing: 'La ejecución no guardó el informe final.',
    html_unavailable: 'El HTML guardado del informe no está disponible o está dañado.',
  });

  // Buttons. `slow`: runs the PDF engine (spinner, long timeout). `confirm`: ask first. `input`: needs text.
  // Order of the buttons is the server's order of `actions`.
  const ACTIONS = Object.freeze({
    prepare: {label: 'Generar PDF', icon: 'file-earmark-pdf', tone: 'secondary', slow: true},
    submit: {label: 'Enviar a revisión', icon: 'send', tone: 'primary', slow: true},
    approve: {label: 'Aprobar', icon: 'check-lg', tone: 'success',
      confirm: '¿Aprobar este PDF? La decisión queda registrada y no puede modificarse.'},
    reject: {label: 'Rechazar', icon: 'x-lg', tone: 'danger', input: 'reason'},
    comment: {label: 'Agregar observación', icon: 'chat-left-text', tone: 'secondary', input: 'comment'},
  });

  // How a failed call reads. Keyed by the server's stable `code`; anything else uses the server's message.
  // `reload`: the screen state is stale, refetch it. `blockApprove`: the PDF cannot be trusted.
  const ERROR_ADVICE = Object.freeze({
    review_state_changed: {message: 'Otra persona ya resolvió esta revisión. Se muestra su estado actual.', reload: true},
    review_conflict: {message: 'La revisión cambió de estado. Se muestra su estado actual.', reload: true},
    pdf_mismatch: {message: 'El PDF que estabas viendo ya no coincide con el registrado. Se recargó la revisión: volvé a mirar el documento antes de decidir.', reload: true},
    preparation_in_progress: {message: 'Otra persona está preparando este informe. Se reintentará en unos segundos.', retry: true},
    pdf_integrity_failed: {message: 'El PDF guardado no coincide con su huella (SHA-256). No se puede aprobar; sí podés rechazarlo.', blockApprove: true, level: 'danger'},
    pdf_not_found: {message: 'No se encontró el PDF de esta revisión.', blockApprove: true, level: 'danger'},
    pdf_too_large: {message: 'El PDF generado supera el tamaño máximo permitido.'},
    pdf_render_failed: {message: 'No se pudo generar el PDF. Intentá nuevamente.'},
    pdf_persist_failed: {message: 'No se pudo guardar el PDF. Intentá nuevamente.'},
    browser_unavailable: {message: 'El motor de PDF no está disponible en este momento.'},
    html_unavailable: {message: ELIGIBILITY_REASONS.html_unavailable},
    run_completed_with_errors: {message: ELIGIBILITY_REASONS.run_completed_with_errors, reload: true},
    run_not_completed: {message: ELIGIBILITY_REASONS.run_not_completed, reload: true},
    reason_required: {message: 'El motivo del rechazo es obligatorio.'},
    comment_required: {message: 'La observación no puede estar vacía.'},
    timeout: {message: 'La operación tardó demasiado. Revisá el estado de la revisión antes de reintentar.', reload: true},
  });

  const DEFAULT_INBOX_STATUSES = Object.freeze(['pending_review']);

  // ── small helpers ──────────────────────────────────────────────────────────────────────────

  const lookup = (table, key, fallback) => (Object.prototype.hasOwnProperty.call(table, key) ? table[key] : fallback);

  function statusInfo(status) {
    return Object.assign({status}, lookup(REVIEW_STATUSES, status, {label: status || '—', tone: 'neutral', icon: 'question-circle'}));
  }

  function runStatusInfo(status) {
    return Object.assign({status}, lookup(RUN_STATUSES, status, {label: status || '—', tone: 'neutral'}));
  }

  function formatDate(iso) {
    if (!iso) return '—';
    const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z'); // naive ISO from the server is UTC
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
  }

  const shortHash = hash => (typeof hash === 'string' && hash ? hash.slice(0, 8) : '');

  function formatSize(bytes) {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function withParam(url, key, value) {
    return url + (url.includes('?') ? '&' : '?') + encodeURIComponent(key) + '=' + encodeURIComponent(value);
  }

  // The person behind an event, from the SNAPSHOT stored with it (it outlives the user row).
  function actorName(event) {
    const actor = (event && event.metadata && event.metadata.actor) || {};
    if (actor.kind === 'system') return actor.username || 'Sistema';
    return actor.username || actor.email || (event && event.actor_user_id ? 'Usuario #' + event.actor_user_id : 'Usuario eliminado');
  }

  // ── inbox ──────────────────────────────────────────────────────────────────────────────────

  // form: {status: string|string[], report_id, date_from, date_to}. Empty values are dropped.
  function buildInboxQuery(form, page, perPage) {
    const params = new URLSearchParams();
    const statuses = [].concat(form && form.status !== undefined ? form.status : DEFAULT_INBOX_STATUSES).filter(Boolean);
    statuses.forEach(status => params.append('status', status));
    ['report_id', 'date_from', 'date_to'].forEach(key => { if (form && form[key]) params.set(key, form[key]); });
    params.set('page', String(Math.max(1, page || 1)));
    if (perPage) params.set('per_page', String(perPage));
    return params;
  }

  // A run that did not end `completed` is NOT eligible: shown apart from a human "rejected".
  function inboxRow(item, detailUrl) {
    const technical = item.run_status && item.run_status !== 'completed' ? runStatusInfo(item.run_status) : null;
    return {
      id: item.id,
      runId: item.run_id,
      title: item.run_name || 'Informe',
      reportName: item.report_name || '—',
      status: statusInfo(item.status),
      technicalNote: technical && {label: 'No apto: ' + technical.label.toLowerCase(), tone: technical.tone},
      requestedBy: item.requested_by || '—',
      submittedBy: item.submitted_by || (item.submitted_at ? 'Sistema' : '—'),
      submittedAt: formatDate(item.submitted_at),
      decidedBy: item.decided_by || null,
      hasPdf: !!item.has_pdf,
      pdfUrl: item.pdf && item.pdf.url ? item.pdf.url : null,
      detailUrl: detailUrl ? detailUrl.replace('{run_id}', encodeURIComponent(item.run_id)) : null,
    };
  }

  function inboxSummary(page) {
    if (!page || !page.total) return 'Sin revisiones para los filtros elegidos.';
    return page.total + (page.total === 1 ? ' revisión' : ' revisiones') + ' · página ' + page.page + ' de ' + page.pages;
  }

  // ── detail ─────────────────────────────────────────────────────────────────────────────────

  function presentTimeline(events) {
    return (events || []).map(event => {
      const kind = lookup(EVENT_TYPES, event.type, {label: event.type || 'Evento', tone: 'neutral', icon: 'circle'});
      const meta = event.metadata || {};
      const transition = event.previous_status !== event.new_status && event.new_status
        ? statusInfo(event.previous_status).label + ' → ' + statusInfo(event.new_status).label : null;
      return {
        id: event.id, type: event.type, label: kind.label, tone: kind.tone, icon: kind.icon,
        actor: actorName(event), at: formatDate(event.created_at), comment: event.comment || null,
        transition, selfReview: meta.self_review === true, source: meta.source || null,
      };
    });
  }

  function eligibilityNote(eligibility) {
    if (!eligibility || eligibility.eligible) return null;
    return {code: eligibility.code, message: lookup(ELIGIBILITY_REASONS, eligibility.code, eligibility.message || 'El informe no es apto para revisión.')};
  }

  // Buttons for the actions the server offered. `blockApprove` removes approve when the PDF is untrusted.
  function presentActions(actions, {blockApprove} = {}) {
    return (actions || [])
      .filter(name => Object.prototype.hasOwnProperty.call(ACTIONS, name) && !(blockApprove && name === 'approve'))
      .map(name => Object.assign({name}, ACTIONS[name]));
  }

  // payload: GET /report-runs/<id>/review; events: GET /reviews/<id>/events (array of event payloads).
  function presentReview({payload, events, blockApprove}) {
    const review = payload.review || null;
    const run = payload.run || null;
    const timeline = presentTimeline(events);
    const submitted = timeline.find(item => item.type === 'submitted');
    const decided = [...timeline].reverse().find(item => item.type === 'approved' || item.type === 'rejected');
    const pdfUrl = review && review.pdf && review.pdf.url ? review.pdf.url : null;
    return {
      runId: payload.run_id,
      title: (run && run.name) || 'Informe',
      reportName: (run && run.report_name) || '—',
      generatedBy: (run && run.requested_by) || '—',
      generatedAt: formatDate(run && run.created_at),
      runStatus: run ? runStatusInfo(run.status) : null,
      hasReview: !!review,
      reviewId: review ? review.id : null,
      status: review ? statusInfo(review.status) : null,
      decided: !!review && (review.status === 'approved' || review.status === 'rejected'),
      rejectionReason: review ? review.rejection_reason || null : null,
      submittedBy: submitted ? submitted.actor : null,
      submittedAt: review ? formatDate(review.submitted_at) : '—',
      decidedBy: decided ? decided.actor : null,
      decidedAt: review ? formatDate(review.decided_at) : '—',
      selfReview: timeline.some(item => item.selfReview),
      pdf: pdfUrl ? {
        url: pdfUrl, downloadUrl: withParam(pdfUrl, 'download', '1'),
        sha256: review.pdf.sha256, shortHash: shortHash(review.pdf.sha256),
        size: formatSize(pdfSize(events)),
      } : null,
      eligibility: eligibilityNote(payload.eligibility),
      actions: presentActions(payload.actions, {blockApprove}),
      timeline,
    };
  }

  // The size the audit recorded for the submitted PDF (no extra request needed).
  function pdfSize(events) {
    const submitted = (events || []).find(event => event.type === 'submitted');
    const pdf = submitted && submitted.metadata && submitted.metadata.pdf;
    return pdf ? pdf.size_bytes : null;
  }

  // How a ReviewApiError should be told to the person and what the screen should do next.
  function adviseError(error) {
    const code = error && error.code;
    const known = code ? lookup(ERROR_ADVICE, code, null) : null;
    const advice = known || {};
    return {
      message: advice.message || (error && error.message) || 'No se pudo completar la operación.',
      level: advice.level || (advice.reload || advice.retry ? 'warning' : 'danger'),
      reload: !!advice.reload || (error && error.kind === 'conflict' && !known),
      retry: !!advice.retry,
      blockApprove: !!advice.blockApprove,
      sessionExpired: !!error && error.kind === 'auth',
    };
  }

  return {
    REVIEW_STATUSES, RUN_STATUSES, EVENT_TYPES, ELIGIBILITY_REASONS, ACTIONS, ERROR_ADVICE, DEFAULT_INBOX_STATUSES,
    statusInfo, runStatusInfo, formatDate, shortHash, formatSize, actorName,
    buildInboxQuery, inboxRow, inboxSummary,
    presentTimeline, presentActions, presentReview, eligibilityNote, adviseError,
  };
});
