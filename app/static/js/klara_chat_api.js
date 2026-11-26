/*
 * Transport of the asynchronous KLARA chat (POST /chat/runs, GET /chat/runs/<id>, POST .../cancel).
 *
 * Only knows HTTP: URLs, JSON and how to classify a failed call. It knows nothing about polling, stages or
 * the DOM (see klara_chat_run.js). Same error contract as klara_reporting_api.js, so the shared run
 * controller (klara_report_run_controller.js) can retry what is worth retrying:
 *
 *   auth       401/403 (the report requires a login the visitor lacks)
 *   not_found  404 (unknown run, or a run of another owner: indistinguishable on purpose)
 *   conflict   409 (`code` chat_run_in_progress carries the open `runId`; chat_run_finished)
 *   transient  network error, 408/429/5xx, or any answer that is not JSON (a proxy's HTML 502/504/524 page):
 *              worth retrying. A proxy error page is NEVER reported as "could not connect".
 *   client     any other 4xx; `message` is the server's public message
 *
 * The chat routes are exempt from CSRF (public report pages); the owner is the signed visitor cookie, so
 * requests are same-origin with credentials.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraChatApi = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const ENDPOINTS = Object.freeze({
    createRun: '/chat/runs',
    getRun: '/chat/runs/{id}',
    cancelRun: '/chat/runs/{id}/cancel',
  });

  const DEFAULT_MESSAGES = Object.freeze({
    auth: 'Necesitás iniciar sesión para consultar este reporte.',
    not_found: 'No encontré la consulta.',
    conflict: 'Ya hay una consulta en curso.',
    transient: 'No pude conectar con el servidor.',
    transient_proxy: 'El servidor tardó demasiado o no está disponible en este momento.',
    client: 'La consulta no es válida.',
  });

  class ChatApiError extends Error {
    constructor(kind, message, {status, code, runId, retryAfterMs} = {}) {
      super(message);
      this.name = 'ChatApiError';
      this.kind = kind;
      this.status = status || 0;
      this.code = code || null;
      this.runId = runId || null;
      this.retryAfterMs = retryAfterMs || null;
    }
  }

  function classify(status) {
    if (status === 401 || status === 403) return 'auth';
    if (status === 404) return 'not_found';
    if (status === 409) return 'conflict';
    if (status === 408 || status === 429 || status >= 500) return 'transient';
    return 'client';
  }

  function isJsonResponse(response) {
    const headers = response && response.headers;
    if (!headers || typeof headers.get !== 'function') return true;  // test doubles without headers
    return /json/i.test(headers.get('Content-Type') || '');
  }

  function retryAfterMs(response) {
    const headers = response && response.headers;
    const raw = headers && typeof headers.get === 'function' ? headers.get('Retry-After') : null;
    const seconds = Number(raw);
    return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : null;
  }

  function createChatApi({fetchImpl, endpoints} = {}) {
    const doFetch = fetchImpl || ((...args) => fetch(...args));
    const urls = Object.assign({}, ENDPOINTS, endpoints || {});
    const withId = (template, id) => template.replace('{id}', encodeURIComponent(id));

    async function request(method, url, {body, signal} = {}) {
      const headers = {Accept: 'application/json'};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      let response;
      try {
        response = await doFetch(url, {
          method, credentials: 'same-origin', headers, signal,
          body: body === undefined ? undefined : JSON.stringify(body),
        });
      } catch (error) {
        if (error && error.name === 'AbortError') throw error;
        throw new ChatApiError('transient', DEFAULT_MESSAGES.transient);
      }
      let data = null;
      if (isJsonResponse(response)) {
        try { data = await response.json(); } catch (_) { data = null; }
      }
      if (response.ok) {
        if (data === null) {
          // An OK page that is not JSON: a login page after a redirect, or a misconfigured proxy.
          throw new ChatApiError(response.redirected ? 'auth' : 'transient',
            response.redirected ? DEFAULT_MESSAGES.auth : DEFAULT_MESSAGES.transient_proxy, {status: response.status});
        }
        return data;
      }
      const kind = classify(response.status);
      const fallback = kind === 'transient' && data === null ? DEFAULT_MESSAGES.transient_proxy : DEFAULT_MESSAGES[kind];
      const message = (data && typeof data.error === 'string' && data.error) || fallback;
      throw new ChatApiError(kind, message, {
        status: response.status, code: data && data.code, runId: data && data.run_id,
        retryAfterMs: retryAfterMs(response),
      });
    }

    return {
      // body: {message, slug, conversation_id, model_key} -> {run_id, status, stage, poll_after_ms}
      createRun(body) { return request('POST', urls.createRun, {body}); },
      getRun(runId, {signal} = {}) { return request('GET', withId(urls.getRun, runId), {signal}); },
      cancelRun(runId) { return request('POST', withId(urls.cancelRun, runId)); },
    };
  }

  return {ChatApiError, classify, createChatApi, ENDPOINTS, DEFAULT_MESSAGES};
});
