/* KLARA · Configuración AI — shared behaviour (no framework, no dependencies).
 *
 *  - Searchable select: progressively enhances any <select data-searchable>. The native
 *    <select> stays in the DOM (hidden) as the single source of truth, so existing code that
 *    reads `select.value` / listens to `change` keeps working untouched.
 *  - Client-side table filters: [data-ai-filter-scope] + [data-filter-row].
 *  - Row details toggles, confirm-on-submit and an unsaved-changes guard.
 */
(function () {
  'use strict';

  const MAX_RENDERED = 100;
  const normalize = value => String(value == null ? '' : value)
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* ------------------------------------------------------------------ */
  /* Searchable select                                                   */
  /* ------------------------------------------------------------------ */
  const instances = new WeakMap();

  function enhanceSelect(select) {
    if (instances.has(select)) return instances.get(select);

    const wrap = el('div', 'ai-ss');
    const control = el('button', 'ai-ss-control');
    control.type = 'button';
    control.setAttribute('aria-haspopup', 'listbox');
    control.setAttribute('aria-expanded', 'false');
    const valueEl = el('span', 'ai-ss-value');
    const clearBtn = el('button', 'ai-ss-clear');
    clearBtn.type = 'button';
    clearBtn.title = 'Limpiar selección';
    clearBtn.setAttribute('aria-label', 'Limpiar selección');
    clearBtn.innerHTML = '<i class="bi bi-x-lg"></i>';
    clearBtn.hidden = true;
    const caret = el('i', 'bi bi-chevron-expand ai-ss-caret');
    control.append(valueEl, clearBtn, caret);

    const panel = el('div', 'ai-ss-panel');
    panel.hidden = true;
    const search = el('input', 'ai-ss-search');
    search.type = 'search';
    search.autocomplete = 'off';
    search.placeholder = select.dataset.searchPlaceholder || 'Buscar…';
    search.setAttribute('aria-label', search.placeholder);
    const list = el('ul', 'ai-ss-list');
    list.setAttribute('role', 'listbox');
    const note = el('div', 'ai-ss-note');
    note.hidden = true;
    panel.append(search, list, note);
    wrap.append(control, panel);

    select.hidden = true;
    select.tabIndex = -1;
    select.after(wrap);

    let items = [];
    let visible = [];
    let activeIndex = -1;

    const placeholder = () => select.dataset.placeholder || 'Seleccionar…';
    const readItems = () => [...select.options].map(option => ({
      value: option.value,
      label: option.textContent.trim(),
      meta: option.dataset.meta || '',
      warn: option.dataset.warning || '',
      disabled: option.disabled,
    }));
    const isClearable = () => select.dataset.clearable !== undefined;

    function syncLabel() {
      const option = select.selectedOptions[0];
      const empty = !select.value;
      valueEl.textContent = option && !(empty && !option.textContent.trim())
        ? option.textContent.trim() : placeholder();
      valueEl.classList.toggle('placeholder', empty);
      clearBtn.hidden = !(isClearable() && !empty && !select.disabled);
      wrap.classList.toggle('is-disabled', select.disabled);
      control.disabled = select.disabled;
    }

    function render() {
      const terms = normalize(search.value).split(/\s+/).filter(Boolean);
      items = readItems();
      const matches = items.filter(item => {
        if (!terms.length) return true;
        const haystack = normalize(`${item.label} ${item.meta}`);
        return terms.every(term => haystack.includes(term));
      });
      visible = matches.slice(0, MAX_RENDERED);
      list.replaceChildren();
      visible.forEach((item, index) => {
        const li = el('li', 'ai-ss-option');
        li.setAttribute('role', 'option');
        li.dataset.index = index;
        if (item.value === select.value) { li.classList.add('selected'); li.setAttribute('aria-selected', 'true'); }
        const label = el('span');
        label.append(document.createTextNode(item.label));
        if (item.meta) label.append(el('span', 'ai-ss-meta', item.meta));
        li.append(label);
        if (item.warn) li.append(el('span', 'ai-ss-warn', `⚠ ${item.warn}`));
        if (item.disabled) li.classList.add('text-muted');
        list.append(li);
      });
      if (!visible.length) {
        note.hidden = false;
        note.className = 'ai-ss-note empty';
        note.textContent = select.dataset.emptyText || 'Sin resultados.';
      } else if (matches.length > visible.length) {
        note.hidden = false;
        note.className = 'ai-ss-note';
        note.textContent = `Mostrando ${visible.length} de ${matches.length}. Seguí escribiendo para acotar.`;
      } else {
        note.hidden = true;
      }
      const selectedAt = visible.findIndex(item => item.value === select.value);
      setActive(terms.length ? 0 : (selectedAt >= 0 ? selectedAt : 0));
    }

    function setActive(index) {
      const nodes = list.children;
      if (!nodes.length) { activeIndex = -1; return; }
      activeIndex = Math.max(0, Math.min(index, nodes.length - 1));
      [...nodes].forEach((node, i) => node.classList.toggle('active', i === activeIndex));
      nodes[activeIndex].scrollIntoView({block: 'nearest'});
    }

    function open() {
      if (select.disabled || !panel.hidden) return;
      panel.hidden = false;
      wrap.classList.add('open');
      control.setAttribute('aria-expanded', 'true');
      search.value = '';
      render();
      search.focus();
    }

    function close(focusControl) {
      if (panel.hidden) return;
      panel.hidden = true;
      wrap.classList.remove('open');
      control.setAttribute('aria-expanded', 'false');
      if (focusControl) control.focus();
    }

    function choose(value) {
      const changed = select.value !== value;
      select.value = value;
      syncLabel();
      close(true);
      if (changed) select.dispatchEvent(new Event('change', {bubbles: true}));
    }

    control.addEventListener('click', event => {
      if (event.target.closest('.ai-ss-clear')) return;
      panel.hidden ? open() : close(true);
    });
    control.addEventListener('keydown', event => {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) { event.preventDefault(); open(); }
    });
    clearBtn.addEventListener('click', event => { event.stopPropagation(); choose(''); });
    search.addEventListener('input', render);
    search.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown') { event.preventDefault(); setActive(activeIndex + 1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); setActive(activeIndex - 1); }
      else if (event.key === 'Enter') {
        event.preventDefault();
        const item = visible[activeIndex];
        if (item && !item.disabled) choose(item.value);
      } else if (event.key === 'Escape') { event.preventDefault(); close(true); }
      else if (event.key === 'Tab') close(false);
    });
    list.addEventListener('mousemove', event => {
      const li = event.target.closest('.ai-ss-option');
      if (li && Number(li.dataset.index) !== activeIndex) setActive(Number(li.dataset.index));
    });
    list.addEventListener('click', event => {
      const li = event.target.closest('.ai-ss-option');
      if (!li) return;
      const item = visible[Number(li.dataset.index)];
      if (item && !item.disabled) choose(item.value);
    });
    document.addEventListener('mousedown', event => { if (!wrap.contains(event.target)) close(false); });
    select.addEventListener('change', syncLabel);
    if (select.id) {
      document.querySelectorAll(`label[for="${CSS.escape(select.id)}"]`)
        .forEach(label => label.addEventListener('click', event => { event.preventDefault(); control.focus(); }));
      control.setAttribute('aria-labelledby', select.id);
    }

    const api = {
      refresh() { syncLabel(); if (!panel.hidden) render(); },
      setLoading(loading) {
        wrap.classList.toggle('is-disabled', !!loading);
        if (loading) { valueEl.textContent = 'Cargando…'; valueEl.classList.add('placeholder'); } else syncLabel();
      },
    };
    instances.set(select, api);
    syncLabel();
    return api;
  }

  function refreshSelect(select) {
    const api = instances.get(select) || enhanceSelect(select);
    api.refresh();
  }

  /* ------------------------------------------------------------------ */
  /* Table filters                                                       */
  /* ------------------------------------------------------------------ */
  function initFilterScope(scope) {
    const rows = [...scope.querySelectorAll('[data-filter-row]')];
    const search = scope.querySelector('[data-filter-search]');
    const controls = [...scope.querySelectorAll('[data-filter]')];
    const chipGroups = [...scope.querySelectorAll('[data-filter-chips]')];
    const counter = scope.querySelector('[data-filter-count]');
    const emptyRow = scope.querySelector('[data-filter-empty]');
    const total = rows.length;

    function matches(row, key, wanted) {
      if (!wanted) return true;
      const have = row.dataset[key];
      if (have === undefined) return false;
      return have.split('|').includes(wanted);
    }

    function apply() {
      const terms = search ? normalize(search.value).split(/\s+/).filter(Boolean) : [];
      let shown = 0;
      rows.forEach(row => {
        let ok = terms.every(term => normalize(row.dataset.search || row.textContent).includes(term));
        for (const control of controls) { if (ok) ok = matches(row, control.dataset.filter, control.value); }
        for (const group of chipGroups) {
          if (ok) ok = matches(row, group.dataset.filterChips, (group.querySelector('.active') || {dataset: {}}).dataset.value || '');
        }
        row.hidden = !ok;
        if (ok) shown += 1;
        if (row.id) {
          scope.querySelectorAll(`[data-detail-for="${CSS.escape(row.id)}"]`).forEach(detail => {
            if (!ok) detail.hidden = true;
          });
        }
      });
      if (counter) counter.textContent = shown === total ? `${total} en total` : `${shown} de ${total}`;
      if (emptyRow) emptyRow.hidden = shown !== 0 || total === 0;
    }

    if (search) search.addEventListener('input', apply);
    controls.forEach(control => control.addEventListener('change', apply));
    chipGroups.forEach(group => group.addEventListener('click', event => {
      const chip = event.target.closest('.ai-chip');
      if (!chip) return;
      group.querySelectorAll('.ai-chip').forEach(node => node.classList.toggle('active', node === chip));
      apply();
    }));
    scope.querySelectorAll('[data-filter-reset]').forEach(button => button.addEventListener('click', () => {
      if (search) search.value = '';
      controls.forEach(control => {
        control.value = '';
        control.dispatchEvent(new Event('change', {bubbles: true}));
      });
      chipGroups.forEach(group => group.querySelectorAll('.ai-chip').forEach((chip, index) => chip.classList.toggle('active', index === 0)));
      apply();
    }));
    apply();
  }

  /* ------------------------------------------------------------------ */
  /* Misc helpers                                                        */
  /* ------------------------------------------------------------------ */
  function initToggles(root) {
    root.addEventListener('click', event => {
      const button = event.target.closest('[data-toggle-detail]');
      if (!button) return;
      const target = document.getElementById(button.dataset.toggleDetail);
      if (!target) return;
      target.hidden = !target.hidden;
      button.setAttribute('aria-expanded', String(!target.hidden));
      const icon = button.querySelector('i');
      if (icon) icon.className = target.hidden ? 'bi bi-chevron-right' : 'bi bi-chevron-down';
    });
  }

  function guardUnsaved(isDirty) {
    window.addEventListener('beforeunload', event => {
      if (!isDirty()) return;
      event.preventDefault();
      event.returnValue = '';
    });
  }

  function init() {
    document.querySelectorAll('select[data-searchable]').forEach(enhanceSelect);
    document.querySelectorAll('[data-ai-filter-scope]').forEach(initFilterScope);
    initToggles(document);
    document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    }));
    document.querySelectorAll('form[data-autosubmit] select, form[data-autosubmit] input[type=date]').forEach(control => {
      control.addEventListener('change', () => control.form.submit());
    });
  }

  window.AiConsole = {enhanceSelect, refreshSelect, guardUnsaved, normalize, el};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
