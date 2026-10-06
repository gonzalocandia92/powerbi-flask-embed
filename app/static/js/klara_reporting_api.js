/*
 * Transport layer of the administrative Reporting screen.
 *
 * Only knows HTTP: URLs, CSRF, JSON, and how to classify a failed call. It does not know
 * about polling, run states or the DOM, so swapping polling for SSE/WebSocket later means
 * replacing how the controller obtains runs, not this contract.
 *
 * Failures are ReportingApiError with a `kind` the caller can act on:
 *   auth       401/403, or an expired session (a login page instead of JSON)
 *   not_found  404
 *   transient  network error, 408/429/5xx: worth retrying when polling
 *   client     any other 4xx (validation, etc.); `message` is the server's public message
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReportingApi = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  class ReportingApiError extends Error {
    constructor(kind, message, status) {
      super(message);
      this.name = 'ReportingApiError';
      this.kind = kind;
      this.status = status || 0;
    }
  }

  const DEFAULT_MESSAGES = {
    auth: 'Tu sesión expiró o no tenés permisos para esta acción. Volvé a iniciar sesión.',
    not_found: 'No se encontró el recurso solicitado.',
    transient: 'No se pudo conectar con el servidor.',
    client: 'La solicitud no es válida.',
  };

  function classify(status) {
    if (status === 401 || status === 403) return 'auth';
    if (status === 404) return 'not_found';
    if (status === 408 || status === 429 || status >= 500) return 'transient';
    return 'client';
  }

  // endpoints: {definitions, definition: '/x/{id}', createRun, getRun: '/x/{id}', cancelRun: '/x/{id}/cancel',
  //            structureDefault: '/x', structure: '/x/{id}/structure', compileStructure: '/x/{id}/compile-structure'}
  function createReportingApi({endpoints, csrf, fetchImpl}) {
    const doFetch = fetchImpl || ((...args) => fetch(...args));
    const withId = (template, id) => template.replace('{id}', encodeURIComponent(id));

    async function request(method, url, {body, signal} = {}) {
      const headers = {Accept: 'application/json', 'X-CSRFToken': csrf};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      let response;
      try {
        response = await doFetch(url, {
          method, credentials: 'same-origin', headers, signal,
          body: body === undefined ? undefined : JSON.stringify(body),
        });
      } catch (error) {
        if (error && error.name === 'AbortError') throw error;
        throw new ReportingApiError('transient', DEFAULT_MESSAGES.transient, 0);
      }
      let data = null;
      try { data = await response.json(); } catch (_) { /* not JSON */ }
      if (response.ok) {
        // An OK answer that is not JSON is the login page served after a redirect.
        if (data === null || response.redirected) throw new ReportingApiError('auth', DEFAULT_MESSAGES.auth, response.status);
        return data;
      }
      const kind = classify(response.status);
      const message = (data && typeof data.error === 'string' && data.error) || DEFAULT_MESSAGES[kind];
      throw new ReportingApiError(kind, message, response.status);
    }

    return {
      // POST /definitions or PUT /definitions/<id>; resolves the saved definition (with question keys).
      saveDefinition({definitionId, payload}) {
        return definitionId
          ? request('PUT', withId(endpoints.definition, definitionId), {body: payload})
          : request('POST', endpoints.definitions, {body: payload});
      },
      // POST /report-runs -> {run_id, status, status_url, definition_id} (HTTP 202)
      createRun({definitionId}) {
        return request('POST', endpoints.createRun, {body: {definition_id: definitionId}});
      },
      getRun(runId, {signal} = {}) {
        return request('GET', withId(endpoints.getRun, runId), {signal});
      },
      cancelRun(runId) {
        return request('POST', withId(endpoints.cancelRun, runId));
      },
      // POST: standard structure for the titles on screen (pure code: no model, no DB, nothing saved).
      defaultStructure(titles) {
        return request('POST', endpoints.structureDefault, {body: {questions: titles.map(title => ({title}))}});
      },
      // GET: how KLARA currently understands the structure prompt (never calls the model).
      getStructure(definitionId) {
        return request('GET', withId(endpoints.structure, definitionId));
      },
      // POST: explicit, possibly paid interpretation; idempotent unless force.
      compileStructure({definitionId, force = false}) {
        return request('POST', withId(endpoints.compileStructure, definitionId), {body: {force: !!force}});
      },
    };
  }

  return {ReportingApiError, createReportingApi, classify};
});
