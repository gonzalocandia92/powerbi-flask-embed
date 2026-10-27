/*
 * Tiny DOM helpers shared by the review screens. Everything takes `doc` (the document) as a parameter so the
 * controllers can be exercised with a fake one in Node.
 *
 * Text is always set with textContent / createElement: model- or user-provided strings (comments, names)
 * can never become markup. No HTML string is ever parsed anywhere in the review screens.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReviewDom = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function el(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // The "status dot + label" indicator of the admin console (same markup as the `status_badge` Jinja macro).
  function statusPill(doc, {label, tone}) {
    const pill = el(doc, 'span', 'ai-status tone-' + (tone || 'neutral'));
    pill.append(el(doc, 'span', 'ai-status-dot'), el(doc, 'span', '', label));
    return pill;
  }

  function show(node, visible) {
    node.classList.toggle('d-none', !visible);
  }

  // level: danger | warning | success | info
  function showAlert(node, text, level) {
    if (!text) { node.textContent = ''; show(node, false); return; }
    node.className = 'alert alert-' + (level || 'danger');
    node.textContent = text;
  }

  function link(doc, {text, href, className, newTab, icon}) {
    const anchor = el(doc, 'a', className || '');
    if (icon) anchor.append(el(doc, 'i', 'bi bi-' + icon + ' me-1'));
    anchor.append(doc.createTextNode(text));
    anchor.href = href;
    if (newTab) { anchor.target = '_blank'; anchor.rel = 'noopener'; }
    return anchor;
  }

  // Looks every id of `ids` ({name: elementId}) up; fails loudly (naming them) when a template lost one.
  function collectRefs(doc, ids) {
    const refs = {};
    const missing = [];
    Object.keys(ids).forEach(name => {
      const node = doc.getElementById(ids[name]);
      if (node) refs[name] = node; else missing.push(ids[name]);
    });
    if (missing.length) throw new Error('Faltan elementos en la pantalla: ' + missing.join(', '));
    return refs;
  }

  return {el, statusPill, show, showAlert, link, collectRefs};
});
