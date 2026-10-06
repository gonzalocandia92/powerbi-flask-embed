/*
 * Client side of the "Descargar PDF" button of the administrative Reporting screen.
 *
 * The browser never sends HTML: it posts the FinalReport JSON that the server already
 * generated (plus the run id, for log correlation). The server validates it again, renders
 * the HTML with the official renderer and returns the PDF.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReportPdf = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const FALLBACK_NAME = 'informe-klara.pdf';
  const GENERIC_ERROR = 'No se pudo generar el PDF. Intentá nuevamente.';

  function buildPayload(finalReport, reportRunId) {
    const payload = {final_report: finalReport};
    if (reportRunId) payload.report_run_id = reportRunId;
    return payload;
  }

  function filenameFromDisposition(header) {
    const match = /filename="([A-Za-z0-9._-]{1,120})"/.exec(header || '');
    return match ? match[1] : FALLBACK_NAME;
  }

  // Resolves {blob, filename}; rejects with an Error whose message is safe to show.
  async function requestPdf({endpoint, csrf, finalReport, reportRunId, fetchImpl}) {
    if (!finalReport) throw new Error('No hay un informe generado para exportar.');
    const doFetch = fetchImpl || fetch;
    let response;
    try {
      response = await doFetch(endpoint, {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify(buildPayload(finalReport, reportRunId)),
      });
    } catch (_) {
      throw new Error('Se perdió la conexión al generar el PDF. Intentá nuevamente.');
    }
    if (!response.ok) {
      let message = GENERIC_ERROR;
      try { message = (await response.json()).error || message; } catch (_) { /* non-JSON error page */ }
      throw new Error(message);
    }
    return {
      blob: await response.blob(),
      filename: filenameFromDisposition(response.headers.get('Content-Disposition')),
    };
  }

  return {buildPayload, filenameFromDisposition, requestPdf, GENERIC_ERROR};
});
