/*
 * Review inbox: filters + paginated table over GET /reviews.
 *
 * `createInboxController` receives everything it touches ({api, view, dom, refs, doc, config}), so it is
 * exercised in Node with fakes. `mount` is the only glue that reads the real document and builds the real API.
 * Rows are plain data from `view.inboxRow`; the controller decides nothing about labels or permissions.
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReviewInbox = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  // Element ids the template must provide (a Python test checks the template against this table).
  const REF_IDS = Object.freeze({
    message: 'ri-message', form: 'ri-filters', status: 'ri-f-status', report: 'ri-f-report', from: 'ri-f-from',
    to: 'ri-f-to', reset: 'ri-reset', refresh: 'ri-refresh', body: 'ri-body', summary: 'ri-summary',
    prev: 'ri-prev', next: 'ri-next',
  });
  const ALL_STATUSES = '';

  function createInboxController({api, view, dom, refs, doc, config}) {
    let page = 1;
    let pages = 1;
    let requestId = 0; // only the newest answer may paint (filters can change while a call is in flight)

    function option(value, text) {
      const node = dom.el(doc, 'option', '', text);
      node.value = value;
      return node;
    }

    function fillFilters() {
      refs.status.replaceChildren(
        option(ALL_STATUSES, 'Todos'),
        ...config.statuses.map(status => option(status, view.statusInfo(status).label)));
      refs.status.value = view.DEFAULT_INBOX_STATUSES[0] || ALL_STATUSES;
      refs.report.replaceChildren(option('', 'Todos los reportes'), ...config.reports.map(r => option(String(r.id), r.name)));
      refs.report.value = '';
    }

    function formValues() {
      return {
        status: refs.status.value ? [refs.status.value] : [],
        report_id: refs.report.value, date_from: refs.from.value, date_to: refs.to.value,
      };
    }

    function setRows(nodes) { refs.body.replaceChildren(...nodes); }

    function messageRow(text, className) {
      const row = dom.el(doc, 'tr');
      const cell = dom.el(doc, 'td', className || 'text-center text-muted py-4', text);
      cell.colSpan = 7;
      row.append(cell);
      return row;
    }

    function renderRow(item) {
      const row = view.inboxRow(item, config.detailUrl);
      const tr = dom.el(doc, 'tr');
      const title = dom.el(doc, 'td');
      title.append(dom.el(doc, 'div', 'ai-cell-title fw-semibold', row.title), dom.el(doc, 'div', 'ai-cell-sub font-monospace', row.runId));
      const status = dom.el(doc, 'td');
      const stack = dom.el(doc, 'div', 'd-flex flex-column gap-1 align-items-start');
      stack.append(dom.statusPill(doc, row.status));
      if (row.technicalNote) stack.append(dom.statusPill(doc, row.technicalNote));
      status.append(stack);
      const submitted = dom.el(doc, 'td');
      submitted.append(dom.el(doc, 'div', '', row.submittedBy), dom.el(doc, 'div', 'ai-cell-sub', row.submittedAt));
      const actions = dom.el(doc, 'td', 'text-end');
      if (row.detailUrl) actions.append(dom.link(doc, {text: 'Abrir', href: row.detailUrl, className: 'ai-btn-primary btn-sm', icon: 'box-arrow-in-right'}));
      if (row.pdfUrl) actions.append(dom.link(doc, {text: 'PDF', href: row.pdfUrl, className: 'ai-btn-secondary btn-sm ms-1', newTab: true, icon: 'file-earmark-pdf'}));
      tr.append(title, dom.el(doc, 'td', '', row.reportName), status, dom.el(doc, 'td', '', row.requestedBy), submitted, actions);
      return tr;
    }

    async function load() {
      const mine = ++requestId;
      dom.showAlert(refs.message, '');
      setRows([messageRow('Cargando revisiones…')]);
      refs.refresh.disabled = true;
      try {
        const data = await api.listReviews(view.buildInboxQuery(formValues(), page, config.perPage));
        if (mine !== requestId) return;
        pages = data.pages || 1;
        page = data.page || page;
        setRows(data.items.length ? data.items.map(renderRow) : [messageRow('No hay revisiones para los filtros elegidos.')]);
        refs.summary.textContent = view.inboxSummary(data);
        refs.prev.disabled = page <= 1;
        refs.next.disabled = page >= pages;
      } catch (error) {
        if (mine !== requestId || (error && error.name === 'AbortError')) return;
        const advice = view.adviseError(error);
        setRows([messageRow(advice.sessionExpired ? 'Sesión no válida.' : 'No se pudo cargar la bandeja.')]);
        dom.showAlert(refs.message, advice.message, advice.level);
        refs.summary.textContent = '';
        refs.prev.disabled = true;
        refs.next.disabled = true;
      } finally {
        if (mine === requestId) refs.refresh.disabled = false;
      }
    }

    function apply() { page = 1; return load(); }

    function init() {
      fillFilters();
      refs.form.addEventListener('submit', event => { if (event.preventDefault) event.preventDefault(); apply(); });
      refs.reset.addEventListener('click', event => { if (event.preventDefault) event.preventDefault(); fillFilters(); refs.from.value = ''; refs.to.value = ''; apply(); });
      refs.refresh.addEventListener('click', () => load());
      refs.prev.addEventListener('click', () => { if (page > 1) { page -= 1; load(); } });
      refs.next.addEventListener('click', () => { if (page < pages) { page += 1; load(); } });
      return load();
    }

    return {init, load, apply, state: () => ({page, pages})};
  }

  // Real-browser glue: ids -> refs, the API client from the page config, then start.
  // `modules` lets a test inject the sibling scripts that the browser finds on `window`.
  function mount({config, csrf, doc, fetchImpl, modules}) {
    const found = Object.assign({KlaraReviewApi: root.KlaraReviewApi, KlaraReviewView: root.KlaraReviewView,
      KlaraReviewDom: root.KlaraReviewDom}, modules);
    const refs = found.KlaraReviewDom.collectRefs(doc, REF_IDS);
    const controller = createInboxController({
      api: found.KlaraReviewApi.createReviewApi({endpoints: config.endpoints, csrf, fetchImpl}),
      view: found.KlaraReviewView, dom: found.KlaraReviewDom, refs, doc, config,
    });
    controller.init();
    return controller;
  }

  return {REF_IDS, createInboxController, mount};
});
