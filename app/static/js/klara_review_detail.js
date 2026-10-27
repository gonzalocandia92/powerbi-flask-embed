/*
 * Review detail screen: the persisted PDF, who did what, and the actions the server offers.
 *
 * Principles:
 *   - The server is the source of truth. Every mutation ends by reloading the review; nothing is patched
 *     locally, and the buttons shown are exactly the `actions` the server returned.
 *   - A decision carries the SHA-256 of the PDF this screen SHOWED when it was last rendered (never re-read
 *     at click time), so a decision can only apply to the document the reviewer had in front of them.
 *   - Which texts, tones and buttons exist lives in klara_review_view.js; this file only wires them.
 *
 * `createDetailController` receives everything it touches, so it is exercised in Node with fakes.
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReviewDetail = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  // Element ids the template must provide (a Python test checks the template against this table).
  const REF_IDS = Object.freeze({
    message: 'rv-message', loading: 'rv-loading', content: 'rv-content', title: 'rv-title', subtitle: 'rv-subtitle',
    runStatus: 'rv-run-status', reviewStatus: 'rv-review-status', selfReview: 'rv-self-review',
    generatedBy: 'rv-generated-by', generatedAt: 'rv-generated-at', submittedBy: 'rv-submitted-by',
    submittedAt: 'rv-submitted-at', decidedBy: 'rv-decided-by', decidedAt: 'rv-decided-at',
    rejection: 'rv-rejection', rejectionReason: 'rv-rejection-reason', eligibility: 'rv-eligibility',
    pdfFrame: 'rv-pdf-frame', pdfEmpty: 'rv-pdf-empty', pdfOpen: 'rv-pdf-open', pdfDownload: 'rv-pdf-download',
    pdfHash: 'rv-pdf-hash', actions: 'rv-actions', rejectPanel: 'rv-reject-panel', rejectReason: 'rv-reject-reason',
    rejectConfirm: 'rv-reject-confirm', rejectCancel: 'rv-reject-cancel', commentPanel: 'rv-comment-panel',
    commentText: 'rv-comment-text', commentSubmit: 'rv-comment-submit', commentCancel: 'rv-comment-cancel',
    timeline: 'rv-timeline',
  });
  const REQUEST_TIMEOUT_MS = 30000;   // ordinary calls; PDF preparation uses config.slowTimeoutMs
  const DEFAULT_RETRY_SECONDS = 5;

  function createDetailController({api, view, dom, refs, doc, config, timers, confirmFn}) {
    const clock = timers || {setTimeout: (fn, ms) => setTimeout(fn, ms)};
    const ask = confirmFn || (message => root.confirm(message));
    const runId = config.runId;
    const state = {model: null, panel: null, busy: null, blockApprove: false, shownPdf: null, ended: false};

    // ── rendering ──────────────────────────────────────────────────────────────────────────

    function setText(node, text) { node.textContent = text === null || text === undefined || text === '' ? '—' : text; }

    function renderHeader(model) {
      setText(refs.title, model.title);
      setText(refs.subtitle, model.reportName + ' · ejecución ' + model.runId);
      refs.runStatus.replaceChildren(...(model.runStatus ? [dom.statusPill(doc, model.runStatus)] : []));
      refs.reviewStatus.replaceChildren(dom.statusPill(doc, model.status || {label: 'Sin enviar a revisión', tone: 'neutral'}));
      dom.show(refs.selfReview, model.selfReview);
      setText(refs.generatedBy, model.generatedBy);
      setText(refs.generatedAt, model.generatedAt);
      setText(refs.submittedBy, model.submittedBy);
      setText(refs.submittedAt, model.hasReview ? model.submittedAt : null);
      setText(refs.decidedBy, model.decidedBy);
      setText(refs.decidedAt, model.decided ? model.decidedAt : null);
      refs.rejectionReason.textContent = model.rejectionReason || '';
      dom.show(refs.rejection, !!model.rejectionReason);
      dom.showAlert(refs.eligibility, model.eligibility ? 'No apto para aprobación: ' + model.eligibility.message : '', 'warning');
    }

    function renderPdf(model) {
      const pdf = model.pdf;
      dom.show(refs.pdfFrame, !!pdf);
      dom.show(refs.pdfEmpty, !pdf);
      dom.show(refs.pdfOpen, !!pdf);
      dom.show(refs.pdfDownload, !!pdf);
      dom.show(refs.pdfHash, !!pdf);
      if (!pdf) {
        refs.pdfEmpty.textContent = model.hasReview
          ? 'Todavía no se generó el PDF de esta revisión. Usá «Generar PDF» o «Enviar a revisión».'
          : 'Este informe aún no entró al flujo de revisión. El PDF se genera al prepararlo o enviarlo.';
        state.shownPdf = null;
        return;
      }
      refs.pdfOpen.href = pdf.url;
      refs.pdfDownload.href = pdf.downloadUrl;
      refs.pdfHash.textContent = 'SHA-256 ' + pdf.shortHash + '…' + (pdf.size ? ' · ' + pdf.size : '');
      refs.pdfHash.title = pdf.sha256;
      const key = pdf.url + '#' + pdf.sha256;
      if (state.shownPdf !== key) { refs.pdfFrame.src = pdf.url; state.shownPdf = key; } // do not reload on every repaint
    }

    function renderTimeline(model) {
      if (!model.timeline.length) {
        refs.timeline.replaceChildren(dom.el(doc, 'li', 'text-muted small', 'Todavía no hay actividad registrada.'));
        return;
      }
      refs.timeline.replaceChildren(...model.timeline.map(item => {
        const li = dom.el(doc, 'li', 'ai-timeline-item');
        const head = dom.el(doc, 'div', 'd-flex flex-wrap align-items-center gap-2');
        head.append(dom.statusPill(doc, {label: item.label, tone: item.tone}), dom.el(doc, 'strong', '', item.actor),
          dom.el(doc, 'span', 'ai-cell-sub', item.at));
        if (item.selfReview) head.append(dom.el(doc, 'span', 'ai-pill tone-warning', 'Autorrevisión'));
        li.append(head);
        if (item.transition) li.append(dom.el(doc, 'div', 'ai-cell-sub', item.transition));
        if (item.comment) li.append(dom.el(doc, 'div', 'ai-timeline-comment', item.comment));
        return li;
      }));
    }

    function renderActions(model) {
      const buttons = model.actions.map(action => {
        const working = state.busy === action.name;
        const label = working && action.slow ? 'Generando PDF…' : action.label;
        const button = dom.el(doc, 'button', 'ai-btn-' + (action.tone === 'success' ? 'primary' : action.tone) + ' btn-sm');
        button.type = 'button';
        button.dataset.action = action.name;
        button.append(dom.el(doc, 'i', 'bi bi-' + action.icon + ' me-1'), doc.createTextNode(label));
        button.disabled = !!state.busy || state.ended;
        button.addEventListener('click', () => perform(action));
        return button;
      });
      refs.actions.replaceChildren(...buttons);
      const offered = name => model.actions.some(a => a.name === name);
      dom.show(refs.rejectPanel, state.panel === 'reject' && offered('reject'));
      dom.show(refs.commentPanel, state.panel === 'comment' && offered('comment'));
      [refs.rejectConfirm, refs.rejectCancel, refs.commentSubmit, refs.commentCancel].forEach(node => { node.disabled = !!state.busy || state.ended; });
    }

    function render() {
      const model = state.model;
      if (!model) return;
      renderHeader(model);
      renderPdf(model);
      renderTimeline(model);
      renderActions(model);
      dom.show(refs.loading, false);
      dom.show(refs.content, true);
    }

    // ── loading ────────────────────────────────────────────────────────────────────────────

    async function load() {
      const payload = await api.getRunReview(runId);
      const events = payload.review ? (await api.listEvents(payload.review.id)).events : [];
      state.model = view.presentReview({payload, events, blockApprove: state.blockApprove});
      if (state.panel && !state.model.actions.some(a => a.name === state.panel)) state.panel = null;
      render();
    }

    function fail(error, {keepMessage} = {}) {
      if (error && error.name === 'AbortError') return;
      const advice = view.adviseError(error);
      if (advice.blockApprove) state.blockApprove = true;
      if (advice.sessionExpired) state.ended = true;
      dom.showAlert(refs.message, advice.message, advice.level);
      dom.show(refs.loading, false);
      if (advice.retry) {
        const seconds = (error && error.retryAfter) || DEFAULT_RETRY_SECONDS;
        clock.setTimeout(() => refresh({keepMessage: true}), seconds * 1000);
      } else if (advice.reload && !keepMessage) {
        return refresh({keepMessage: true});
      }
      if (state.model) { state.model = reapplyBlock(); render(); }
    }

    // A late integrity failure removes "approve" from what the screen offers, without waiting for a reload.
    function reapplyBlock() {
      const model = state.model;
      return Object.assign({}, model, {actions: model.actions.filter(a => !(state.blockApprove && a.name === 'approve'))});
    }

    // Reload the server state; report its failure without recursing into another reload.
    async function refresh({keepMessage} = {}) {
      try {
        await load();
        if (!keepMessage) dom.showAlert(refs.message, '');
      } catch (error) {
        fail(error, {keepMessage: true});
      }
    }

    // ── actions ────────────────────────────────────────────────────────────────────────────

    function setBusy(name) { state.busy = name; if (state.model) renderActions(state.model); }

    // Runs one call, then reloads. `ok` is the success message. Returns whether it succeeded.
    async function mutate(name, call, ok) {
      setBusy(name);
      dom.showAlert(refs.message, '');
      try {
        await call();
      } catch (error) {
        setBusy(null);
        await fail(error);
        return false;
      }
      state.busy = null;
      try {
        await load();
        dom.showAlert(refs.message, ok, 'success');
      } catch (error) {
        fail(error, {keepMessage: true});
      }
      return true;
    }

    function shownHash() { return state.model && state.model.pdf ? state.model.pdf.sha256 : ''; }

    function perform(action) {
      if (state.busy || state.ended) return undefined;
      const slow = {timeoutMs: config.slowTimeoutMs};
      switch (action.name) {
        case 'prepare': return mutate('prepare', () => api.prepare(runId, slow), 'PDF generado.');
        case 'submit': return mutate('submit', () => api.submit(runId, slow), 'Informe enviado a revisión.');
        case 'approve': {
          if (action.confirm && !ask(action.confirm)) return undefined;
          const reviewId = state.model.reviewId;
          const hash = shownHash();
          return mutate('approve', () => api.approve(reviewId, hash, {timeoutMs: REQUEST_TIMEOUT_MS}), 'Informe aprobado.');
        }
        default: return openPanel(action.input === 'reason' ? 'reject' : action.input === 'comment' ? 'comment' : null);
      }
    }

    function openPanel(name) {
      state.panel = state.panel === name ? null : name;
      if (state.model) renderActions(state.model);
      return undefined;
    }

    function closePanel() { state.panel = null; if (state.model) renderActions(state.model); }

    async function confirmReject() {
      if (state.busy || state.ended) return;
      const reason = refs.rejectReason.value;
      if (!reason.trim()) { dom.showAlert(refs.message, view.ERROR_ADVICE.reason_required.message, 'warning'); return; }
      const reviewId = state.model.reviewId;
      const hash = shownHash();
      if (await mutate('reject', () => api.reject(reviewId, reason, hash, {timeoutMs: REQUEST_TIMEOUT_MS}), 'Informe rechazado.')) {
        refs.rejectReason.value = '';
        state.panel = null;
        if (state.model) renderActions(state.model);
      }
    }

    async function submitComment() {
      if (state.busy || state.ended) return;
      const text = refs.commentText.value;
      if (!text.trim()) { dom.showAlert(refs.message, view.ERROR_ADVICE.comment_required.message, 'warning'); return; }
      const reviewId = state.model.reviewId;
      if (await mutate('comment', () => api.comment(reviewId, text, {timeoutMs: REQUEST_TIMEOUT_MS}), 'Observación registrada.')) {
        refs.commentText.value = '';
      }
    }

    function init() {
      refs.rejectConfirm.addEventListener('click', confirmReject);
      refs.rejectCancel.addEventListener('click', closePanel);
      refs.commentSubmit.addEventListener('click', submitComment);
      refs.commentCancel.addEventListener('click', closePanel);
      return refresh();
    }

    return {init, refresh, perform, confirmReject, submitComment, state: () => state};
  }

  // `modules` lets a test inject the sibling scripts that the browser finds on `window`.
  function mount({config, csrf, doc, fetchImpl, modules, timers, confirmFn}) {
    const found = Object.assign({KlaraReviewApi: root.KlaraReviewApi, KlaraReviewView: root.KlaraReviewView,
      KlaraReviewDom: root.KlaraReviewDom}, modules);
    const refs = found.KlaraReviewDom.collectRefs(doc, REF_IDS);
    const controller = createDetailController({
      api: found.KlaraReviewApi.createReviewApi({endpoints: config.endpoints, csrf, fetchImpl}),
      view: found.KlaraReviewView, dom: found.KlaraReviewDom, refs, doc, config, timers, confirmFn,
    });
    controller.init();
    return controller;
  }

  return {REF_IDS, createDetailController, mount};
});
