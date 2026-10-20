/*
 * Which PDF the generation screen's "Descargar PDF" button hands out [M8].
 *
 * The screen's own button renders a NEW PDF from the report (a draft: never reviewed, and not necessarily
 * byte-identical to the one under review). Once a run has a review with a persisted PDF, staff must get THAT
 * file. This module decides which one applies and what the button says; the screen only obeys. It is pure, and
 * knows nothing about review states: the caller passes `statusLabel` (the review screens' own table), so there
 * is a single place that words a status.
 *
 *   review     {id, status, has_pdf} | null   the summary the run payload already carries
 *   entry      {canReview, pdfUrl, detailUrl} | null   server-provided URL templates (`{id}` / `{run_id}`);
 *              null when the review feature is not installed: everything degrades to the draft behavior
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReportPdfSource = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const DRAFT_LABEL = 'Descargar borrador (no revisado)';
  const DRAFT_HINT = 'Se genera de nuevo desde el informe. No es el PDF que se envía a revisión.';

  const identity = status => status;

  function fill(template, params) {
    return template.replace(/\{(\w+)\}/g, (_, key) => encodeURIComponent(params[key]));
  }

  function resolvePdfSource({review, entry, statusLabel}) {
    const label = statusLabel || identity;
    if (review && review.has_pdf && entry && entry.canReview && entry.pdfUrl) {
      const url = fill(entry.pdfUrl, {id: review.id});
      return {
        kind: 'reviewed',
        label: 'Descargar PDF · ' + label(review.status),
        hint: 'Es el archivo exacto guardado en la revisión.',
        href: url + (url.includes('?') ? '&' : '?') + 'download=1',
      };
    }
    return {kind: 'draft', label: DRAFT_LABEL, hint: DRAFT_HINT, href: null};
  }

  // The link to the review screen: only for staff and only for a run that finished `completed`.
  function resolveReviewLink({review, runStatus, runId, entry, statusLabel}) {
    if (!entry || !entry.canReview || !entry.detailUrl || !runId || runStatus !== 'completed') return null;
    const label = statusLabel || identity;
    return {
      href: fill(entry.detailUrl, {run_id: runId}),
      label: review ? 'Ver revisión · ' + label(review.status) : 'Enviar a revisión',
    };
  }

  return {DRAFT_LABEL, DRAFT_HINT, resolvePdfSource, resolveReviewLink};
});
