/*
 * Presentation model of the scheduled-reports screen. Pure: no DOM, no HTTP.
 *
 * Everything a person reads lives in the DATA TABLES below, keyed by the stable values the server sends
 * (frequency, policy, occurrence status, error code). A new value is one more row, and works meanwhile: unknown
 * values fall back to a neutral presentation showing the raw value. A Python test checks the tables cover the
 * server vocabularies (persistence/schedule_states.py).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraSchedulesView = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const WEEKDAYS = Object.freeze(['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']);

  const FREQUENCIES = Object.freeze({
    weekly: {label: 'Semanal', hint: 'Cada ejecución analiza la semana anterior completa (lunes a domingo).'},
    monthly: {label: 'Mensual', hint: 'Cada ejecución analiza el mes calendario anterior completo.'},
  });

  const FRESHNESS_POLICIES = Object.freeze({
    import_refresh_after_period_end: {label: 'Esperar un refresh exitoso posterior al cierre del período (Import)'},
    none: {label: 'Sin espera (DirectQuery / Live)'},
  });

  const MISFIRE_POLICIES = Object.freeze({
    latest_within_grace: {label: 'Si se perdió el horario, ejecutar la más reciente dentro de la ventana de gracia'},
    skip: {label: 'Solo ejecutar a horario; si se perdió, omitir'},
  });

  const OCCURRENCE_STATUSES = Object.freeze({
    waiting_data: {label: 'Esperando datos', tone: 'info', icon: 'hourglass-split'},
    ready: {label: 'Lista para ejecutar', tone: 'primary', icon: 'play-circle'},
    enqueued: {label: 'En ejecución', tone: 'primary', icon: 'gear'},
    submitted: {label: 'Enviada a revisión', tone: 'success', icon: 'patch-check'},
    run_failed: {label: 'Ejecución fallida', tone: 'danger', icon: 'x-circle'},
    expired: {label: 'Vencida sin datos', tone: 'warning', icon: 'clock-history'},
    blocked: {label: 'Bloqueada', tone: 'danger', icon: 'slash-circle'},
    skipped: {label: 'Omitida', tone: 'neutral', icon: 'skip-forward'},
  });

  const ERROR_CODES = Object.freeze({
    refresh_not_after_period_end: 'Todavía no hay un refresh exitoso posterior al cierre del período.',
    refresh_in_progress: 'Hay un refresh de Power BI en curso.',
    refresh_failed: 'El último refresh de Power BI falló.',
    refresh_history_empty: 'Power BI no devolvió historial de refresh.',
    refresh_deadline_exceeded: 'Venció la espera de datos: no se generó el informe.',
    definition_not_runnable: 'La definición del informe está inactiva o no tiene preguntas.',
    snapshot_unreadable: 'La ocurrencia no tiene un snapshot válido.',
    missed_beyond_grace: 'Se perdió el horario y quedó fuera de la ventana de gracia.',
    superseded_by_later_occurrence: 'Omitida: se ejecutó una ocurrencia posterior.',
    powerbi_unauthorized: 'Power BI rechazó las credenciales.',
    powerbi_credentials_missing: 'Faltan credenciales de Power BI.',
    powerbi_not_found: 'Power BI no encontró el report o el dataset.',
    powerbi_unavailable: 'Power BI no respondió; se vuelve a intentar.',
    powerbi_throttled: 'Power BI limitó las consultas; se vuelve a intentar.',
    review_submission_failed: 'No se pudo enviar a revisión; podés hacerlo desde la bandeja.',
    run_missing: 'La ocurrencia no tiene una ejecución asociada.',
  });

  // Liveness of the report-scheduler process (GET /schedules -> `scheduler.status`). `null`: nothing to warn about.
  const SCHEDULER_HEALTH = Object.freeze({
    ok: null,
    degraded: {tone: 'warning', text: 'El proceso report-scheduler está activo, pero su última pasada falló'},
    stale: {tone: 'danger', text: 'El proceso report-scheduler no da señales de vida: las programaciones no avanzan'},
    never_seen: {tone: 'warning', text: 'El proceso report-scheduler nunca reportó actividad: las programaciones no se ejecutan hasta que corra'},
    unknown: {tone: 'secondary', text: 'No se pudo comprobar el estado del proceso report-scheduler'},
  });

  const has = (table, key) => typeof key === 'string' && Object.prototype.hasOwnProperty.call(table, key);
  const lookup = (table, key, fallback) => (has(table, key) ? table[key] : fallback);

  function statusInfo(status) {
    if (!status) return {status, label: '—', tone: 'neutral', icon: 'circle'};
    return Object.assign({status}, lookup(OCCURRENCE_STATUSES, status, {label: status, tone: 'neutral', icon: 'circle'}));
  }

  function errorText(error) {
    if (!error || !error.code) return null;
    if (has(ERROR_CODES, error.code)) return ERROR_CODES[error.code];
    if (error.code.indexOf('run_') === 0) return 'La ejecución terminó como «' + error.code.slice(4) + '».';
    return error.message || error.code;
  }

  function formatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('es-AR', {dateStyle: 'short', timeStyle: 'short'});
  }

  function formatLocalDate(isoDate) {
    const match = typeof isoDate === 'string' && /^(\d{4})-(\d{2})-(\d{2})/.exec(isoDate);
    return match ? match[3] + '/' + match[2] + '/' + match[1] : '—';
  }

  // "2026-10-26T02:00" (a wall time the server already put in the schedule's timezone) -> "26/10/2026 02:00".
  function formatLocalDateTime(value) {
    const match = typeof value === 'string' && /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})/.exec(value);
    return match ? match[3] + '/' + match[2] + '/' + match[1] + ' ' + match[4] : '—';
  }

  function formatDuration(seconds) {
    if (typeof seconds !== 'number' || seconds < 0) return '—';
    if (seconds < 60) return seconds + ' s';
    const minutes = Math.floor(seconds / 60);
    return minutes < 60 ? minutes + ' min ' + (seconds % 60) + ' s' : Math.floor(minutes / 60) + ' h ' + (minutes % 60) + ' min';
  }

  function formatCost(value) {
    const number = Number(value);
    if (value === null || value === undefined || value === '' || Number.isNaN(number)) return '—';
    return 'US$ ' + number.toFixed(4);
  }

  function describeCalendar(schedule) {
    const frequency = lookup(FREQUENCIES, schedule.frequency, {label: schedule.frequency || '—'});
    const when = schedule.frequency === 'weekly'
      ? 'los ' + (WEEKDAYS[schedule.weekday] || '¿día?')
      : schedule.frequency === 'monthly' ? 'el día ' + schedule.day_of_month : '';
    return frequency.label + ' · ' + when + ' a las ' + (schedule.local_time || '—') + ' (' + (schedule.timezone || '—') + ')';
  }

  // Form values (strings, as read from inputs) -> request body. Only the field of the chosen frequency is sent.
  function formToPayload(values) {
    const integer = value => (value === '' || value === null || value === undefined ? null : Number.parseInt(value, 10));
    const payload = {
      name: (values.name || '').trim(), frequency: values.frequency, local_time: values.local_time,
      timezone: (values.timezone || '').trim(), freshness_policy: values.freshness_policy,
      misfire_policy: values.misfire_policy,
      refresh_deadline_hours: integer(values.refresh_deadline_hours),
      misfire_grace_hours: integer(values.misfire_grace_hours),
      weekday: values.frequency === 'weekly' ? integer(values.weekday) : null,
      day_of_month: values.frequency === 'monthly' ? integer(values.day_of_month) : null,
      // One number per line; the server normalizes and validates them.
      whatsapp_recipients: String(values.whatsapp_recipients || '').split(/[\n,;]+/)
        .map(line => line.trim()).filter(Boolean),
    };
    if (values.definition_id !== undefined && values.definition_id !== '') payload.definition_id = integer(values.definition_id);
    return payload;
  }

  function scheduleToForm(schedule) {
    const text = value => (value === null || value === undefined ? '' : String(value));
    return {
      definition_id: text(schedule.definition_id), name: text(schedule.name), frequency: text(schedule.frequency),
      weekday: text(schedule.weekday), day_of_month: text(schedule.day_of_month), local_time: text(schedule.local_time),
      timezone: text(schedule.timezone), freshness_policy: text(schedule.freshness_policy),
      misfire_policy: text(schedule.misfire_policy), refresh_deadline_hours: text(schedule.refresh_deadline_hours),
      misfire_grace_hours: text(schedule.misfire_grace_hours),
      whatsapp_recipients: (schedule.whatsapp_recipients || []).join('\n'),
    };
  }

  function presentPreview(item) {
    const period = item.period || {};
    return {
      when: formatLocalDateTime(item.scheduled_local),
      period: period.label || '—',
      comparison: period.comparison_label ? 'Comparado con ' + period.comparison_label : '',
    };
  }

  function presentSchedule(schedule) {
    return {
      id: schedule.id, name: schedule.name, definition: schedule.definition_name || ('Definición #' + schedule.definition_id),
      report: schedule.report_name || '—', calendar: describeCalendar(schedule),
      nextRun: schedule.is_active ? formatLocalDateTime(schedule.next_run_local) : 'Pausada',
      active: !!schedule.is_active, stateLabel: schedule.is_active ? 'Activa' : 'Pausada',
      stateTone: schedule.is_active ? 'success' : 'neutral',
    };
  }

  function fill(template, params) {
    return template ? template.replace(/\{(\w+)\}/g, (_, key) => encodeURIComponent(params[key])) : null;
  }

  // urls: {runUrl: '...{run_id}', reviewUrl: '...{run_id}...' | null}
  function presentOccurrence(item, urls) {
    const run = item.run || null;
    const status = statusInfo(item.status);
    const period = item.period || {};
    return {
      id: item.id, status,
      scheduled: formatDate(item.scheduled_for),
      manual: item.trigger_source === 'manual',
      period: period.label || (period.start ? formatLocalDate(period.start) + ' – ' + formatLocalDate(period.end) : '—'),
      refresh: item.refresh_completed_at ? 'Refresh ' + formatDate(item.refresh_completed_at)
        : item.refresh_checked_at ? 'Comprobado ' + formatDate(item.refresh_checked_at) + ' (' + item.check_attempts + ')' : '—',
      run: run ? {
        id: run.id, status: run.status, url: fill(urls && urls.runUrl, {run_id: run.id}),
        started: formatDate(run.started_at), duration: formatDuration(run.duration_seconds), cost: formatCost(run.cost_usd),
        attempts: item.run_attempts || 1,
      } : null,
      review: run && run.review && urls && urls.reviewUrl
        ? {status: run.review.status, url: fill(urls.reviewUrl, {run_id: run.id})} : null,
      error: errorText(item.error),
      canRetry: !!item.can_retry,
      // Retrying a failed run creates a NEW run and spends AI tokens again: ask first.
      retryNeedsConfirm: item.status === 'run_failed',
    };
  }

  // Server health -> {tone, text} for a banner, or null when the scheduler is fine (or the server sent nothing).
  function presentSchedulerHealth(health) {
    if (!health || !health.status) return null;
    const entry = lookup(SCHEDULER_HEALTH, health.status,
      {tone: 'secondary', text: 'Estado del proceso report-scheduler: ' + health.status});
    if (!entry) return null;
    const parts = [entry.text];
    if (health.status === 'degraded' && health.failed_phases && health.failed_phases.length) {
      parts[0] += ' (' + health.failed_phases.join(', ') + ')';
    }
    if (health.last_seen_at) parts.push('Última señal: ' + formatDate(health.last_seen_at) + '.');
    return {tone: entry.tone, text: parts[0] + '.' + (parts.length > 1 ? ' ' + parts.slice(1).join(' ') : '')};
  }

  function presentCounts(counts) {
    return Object.keys(counts || {}).sort().map(status => Object.assign({count: counts[status]}, statusInfo(status)));
  }

  return {
    WEEKDAYS, FREQUENCIES, FRESHNESS_POLICIES, MISFIRE_POLICIES, OCCURRENCE_STATUSES, ERROR_CODES, SCHEDULER_HEALTH,
    statusInfo, errorText, formatDate, formatLocalDate, formatLocalDateTime, formatDuration, formatCost, describeCalendar,
    formToPayload, scheduleToForm, presentPreview, presentSchedule, presentOccurrence, presentCounts,
    presentSchedulerHealth,
  };
});
