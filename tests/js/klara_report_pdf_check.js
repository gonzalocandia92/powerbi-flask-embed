const assert = require('assert');
const PDF = require('../../app/static/js/klara_report_pdf.js');

(async () => {
  const finalReport = {schema_version: '1.1', title: 'Informe', executive_summary: {headline: 'x'}, sections: []};

  // Payload: the FinalReport already on screen (+ run id); never HTML.
  const payload = PDF.buildPayload(finalReport, 'run-1');
  assert.deepStrictEqual(Object.keys(payload).sort(), ['final_report', 'report_run_id']);
  assert.strictEqual(payload.final_report, finalReport);
  assert(!JSON.stringify(payload).includes('<html'));
  assert.deepStrictEqual(Object.keys(PDF.buildPayload(finalReport, null)), ['final_report']);

  assert.strictEqual(PDF.filenameFromDisposition('attachment; filename="informe-ventas.pdf"'), 'informe-ventas.pdf');
  assert.strictEqual(PDF.filenameFromDisposition('attachment; filename="../../x"'), 'informe-klara.pdf');
  assert.strictEqual(PDF.filenameFromDisposition(null), 'informe-klara.pdf');

  // Success: POSTs exactly that payload with the CSRF header, returns blob + filename.
  let sent;
  const ok = async (url, options) => {
    sent = {url, options};
    return {ok: true, blob: async () => 'BLOB', headers: {get: () => 'attachment; filename="informe-a.pdf"'}};
  };
  const result = await PDF.requestPdf({endpoint: '/pdf', csrf: 'tok', finalReport, reportRunId: 'run-1', fetchImpl: ok});
  assert.deepStrictEqual(result, {blob: 'BLOB', filename: 'informe-a.pdf'});
  assert.strictEqual(sent.url, '/pdf');
  assert.strictEqual(sent.options.method, 'POST');
  assert.strictEqual(sent.options.headers['X-CSRFToken'], 'tok');
  assert.deepStrictEqual(JSON.parse(sent.options.body), {final_report: finalReport, report_run_id: 'run-1'});

  // Errors: readable server message, generic fallback, network loss; nothing internal leaks.
  const failing = body => async () => ({ok: false, json: async () => { if (body === null) throw new Error('html'); return body; }});
  await assert.rejects(PDF.requestPdf({endpoint: '/pdf', csrf: 't', finalReport, fetchImpl: failing({error: 'PDF no disponible', code: 'browser_unavailable'})}), /PDF no disponible/);
  await assert.rejects(PDF.requestPdf({endpoint: '/pdf', csrf: 't', finalReport, fetchImpl: failing(null)}), new RegExp(PDF.GENERIC_ERROR));
  await assert.rejects(PDF.requestPdf({endpoint: '/pdf', csrf: 't', finalReport, fetchImpl: async () => { throw new TypeError('Failed to fetch'); }}), /conexión/);
  await assert.rejects(PDF.requestPdf({endpoint: '/pdf', csrf: 't', finalReport: null, fetchImpl: ok}), /No hay un informe/);
  console.log('OK');
})().catch(error => { console.error(error); process.exit(1); });
