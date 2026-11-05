/*
 * Transport layer of the scheduled-reports screen. Only knows HTTP: URL templates ({id}), CSRF, JSON, and how
 * to classify a failure. It keeps the server's stable `code` (e.g. `occurrence_not_retryable`, `invalid_rule`).
 *
 * Failures are SchedulesApiError with a `kind`:
 *   auth 401/403 or an expired session (a login page instead of JSON) · not_found 404 · conflict 409
 *   transient network error / 408 / 429 / 5xx (generic message unless the server sent a coded one)
 *   client any other 4xx (validation): `message` is the server's public message
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraSchedulesApi = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  class SchedulesApiError extends Error {
    constructor(kind, message, status, code) {
      super(message);
      this.name = 'SchedulesApiError';
      this.kind = kind;
      this.status = status || 0;
      this.code = code || null;
    }
  }

  const DEFAULT_MESSAGES = {
    auth: 'Tu sesión expiró o no tenés permisos de administrador. Volvé a iniciar sesión.',
    not_found: 'No se encontró el recurso solicitado.',
    conflict: 'El estado cambió. Se actualizó la pantalla.',
    transient: 'No se pudo completar la operación. Intentá nuevamente.',
    client: 'La solicitud no es válida.',
  };

  function classify(status) {
    if (status === 401 || status === 403) return 'auth';
    if (status === 404) return 'not_found';
    if (status === 409) return 'conflict';
    if (status === 408 || status === 429 || status >= 500) return 'transient';
    return 'client';
  }

  function createSchedulesApi({endpoints, csrf, fetchImpl}) {
    const doFetch = fetchImpl || ((...args) => fetch(...args));
    const withId = (template, id) => template.replace('{id}', encodeURIComponent(id));

    async function request(method, url, body) {
      const headers = {Accept: 'application/json', 'X-CSRFToken': csrf};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      let response;
      try {
        response = await doFetch(url, {method, credentials: 'same-origin', headers,
          body: body === undefined ? undefined : JSON.stringify(body)});
      } catch (_) {
        throw new SchedulesApiError('transient', DEFAULT_MESSAGES.transient, 0);
      }
      let data = null;
      try { data = await response.json(); } catch (_) { /* not JSON */ }
      if (response.ok) {
        if (data === null || response.redirected) throw new SchedulesApiError('auth', DEFAULT_MESSAGES.auth, response.status, 'login_required');
        return data;
      }
      const kind = classify(response.status);
      const payload = data && typeof data === 'object' ? data : {};
      const code = typeof payload.code === 'string' ? payload.code : null;
      const message = (kind === 'transient' && !code) ? DEFAULT_MESSAGES.transient
        : ((typeof payload.error === 'string' && payload.error) || DEFAULT_MESSAGES[kind]);
      throw new SchedulesApiError(kind, message, response.status, code);
    }

    return {
      list() { return request('GET', endpoints.list); },
      get(id) { return request('GET', withId(endpoints.schedule, id)); },
      create(payload) { return request('POST', endpoints.create, payload); },
      update(id, payload) { return request('PUT', withId(endpoints.schedule, id), payload); },
      pause(id) { return request('POST', withId(endpoints.pause, id)); },
      resume(id) { return request('POST', withId(endpoints.resume, id)); },
      preview(payload) { return request('POST', endpoints.preview, payload); },
      occurrences(id, page) { return request('GET', withId(endpoints.occurrences, id) + '?page=' + encodeURIComponent(page || 1)); },
      runNow(id) { return request('POST', withId(endpoints.runNow, id)); },
      retry(occurrenceId) { return request('POST', withId(endpoints.retry, occurrenceId)); },
    };
  }

  return {SchedulesApiError, createSchedulesApi, classify};
});
