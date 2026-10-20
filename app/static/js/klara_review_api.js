/*
 * Transport layer of the report-review screens (inbox and detail).
 *
 * Only knows HTTP: URL templates, CSRF, JSON, and how to classify a failed call. It does not know review
 * states, texts or the DOM. Unlike the generation screen's client it KEEPS the server's stable `code` (and the
 * extra fields of the error body, in `details`), because the review UI reacts to codes such as
 * `preparation_in_progress` or `pdf_integrity_failed`. It is deliberately independent of
 * klara_reporting_api.js: reviewing must not depend on the generation screen, nor the other way round.
 *
 * Failures are ReviewApiError with a `kind` the caller can act on:
 *   auth       401/403, or an expired session (a login page instead of JSON)
 *   not_found  404
 *   conflict   409 (state changed, hash differs, preparation in progress, run not eligible)
 *   transient  network error, timeout, 408/429/5xx: `message` is generic unless the server sent a public one
 *   client     any other 4xx (validation): `message` is the server's public message
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReviewApi = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  class ReviewApiError extends Error {
    constructor(kind, message, status, code, details) {
      super(message);
      this.name = 'ReviewApiError';
      this.kind = kind;
      this.status = status || 0;
      this.code = code || null;
      this.details = details || {};
      this.retryAfter = 0;
    }
  }

  const DEFAULT_MESSAGES = {
    auth: 'Tu sesión expiró o no tenés permisos para esta acción. Volvé a iniciar sesión.',
    not_found: 'No se encontró el recurso solicitado.',
    conflict: 'El estado de la revisión cambió. Se actualizó la pantalla.',
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

  // endpoints: URL templates with `{id}` (review id) / `{run_id}`: inbox, runReview, prepare, submit,
  //            approve, reject, comments, events. `pdf` is not requested here: the browser loads it.
  function createReviewApi({endpoints, csrf, fetchImpl, timers}) {
    const doFetch = fetchImpl || ((...args) => fetch(...args));
    const clock = timers || {setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id)};
    const fill = (template, params) => template.replace(/\{(\w+)\}/g, (_, key) => encodeURIComponent(params[key]));

    // timeoutMs: 0 = none. Slow calls (PDF rendering) pass a value above the server's own timeout.
    async function request(method, url, {body, signal, timeoutMs} = {}) {
      const headers = {Accept: 'application/json', 'X-CSRFToken': csrf};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      let controller = null;
      let timer = null;
      let timedOut = false;
      let effectiveSignal = signal;
      if (timeoutMs && typeof AbortController !== 'undefined') {
        controller = new AbortController();
        effectiveSignal = controller.signal;
        if (signal) signal.addEventListener('abort', () => controller.abort());
        timer = clock.setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
      }
      let response;
      try {
        response = await doFetch(url, {
          method, credentials: 'same-origin', headers, signal: effectiveSignal,
          body: body === undefined ? undefined : JSON.stringify(body),
        });
      } catch (error) {
        if (timedOut) throw new ReviewApiError('transient', 'La operación tardó demasiado. Revisá el estado e intentá nuevamente.', 0, 'timeout');
        if (error && error.name === 'AbortError') throw error;
        throw new ReviewApiError('transient', DEFAULT_MESSAGES.transient, 0);
      } finally {
        if (timer !== null) clock.clearTimeout(timer);
      }
      let data = null;
      try { data = await response.json(); } catch (_) { /* not JSON */ }
      if (response.ok) {
        // An OK answer that is not JSON is the login page served after a redirect.
        if (data === null || response.redirected) throw new ReviewApiError('auth', DEFAULT_MESSAGES.auth, response.status, 'login_required');
        return data;
      }
      const kind = classify(response.status);
      const body_ = data && typeof data === 'object' ? data : {};
      const publicMessage = typeof body_.error === 'string' && body_.error;
      // A 5xx body is never surfaced unless the server marked it as a public, coded error.
      const message = (kind === 'transient' && !body_.code) ? DEFAULT_MESSAGES.transient : (publicMessage || DEFAULT_MESSAGES[kind]);
      const {error: _error, code, ...details} = body_;
      const failure = new ReviewApiError(kind, message, response.status, typeof code === 'string' ? code : null, details);
      const retry = response.headers && response.headers.get ? Number(response.headers.get('Retry-After')) : 0;
      failure.retryAfter = Number.isFinite(retry) && retry > 0 ? retry : 0;
      throw failure;
    }

    return {
      // GET: paginated inbox. `query` is a URLSearchParams-compatible string or object.
      listReviews(query, opts) {
        const qs = query instanceof URLSearchParams ? query.toString() : new URLSearchParams(query || {}).toString();
        return request('GET', endpoints.inbox + (qs ? '?' + qs : ''), opts);
      },
      // GET: {run_id, run, review|null, eligibility|null, actions}
      getRunReview(runId, opts) { return request('GET', fill(endpoints.runReview, {run_id: runId}), opts); },
      // POST: idempotent; they run the PDF engine, hence the long timeout the caller must pass.
      prepare(runId, opts) { return request('POST', fill(endpoints.prepare, {run_id: runId}), opts); },
      submit(runId, opts) { return request('POST', fill(endpoints.submit, {run_id: runId}), opts); },
      approve(reviewId, expectedPdfSha256, opts) {
        return request('POST', fill(endpoints.approve, {id: reviewId}), {...opts, body: {expected_pdf_sha256: expectedPdfSha256}});
      },
      reject(reviewId, reason, expectedPdfSha256, opts) {
        return request('POST', fill(endpoints.reject, {id: reviewId}),
          {...opts, body: {reason, expected_pdf_sha256: expectedPdfSha256}});
      },
      comment(reviewId, comment, opts) {
        return request('POST', fill(endpoints.comments, {id: reviewId}), {...opts, body: {comment}});
      },
      listEvents(reviewId, opts) { return request('GET', fill(endpoints.events, {id: reviewId}), opts); },
    };
  }

  return {ReviewApiError, createReviewApi, classify};
});
