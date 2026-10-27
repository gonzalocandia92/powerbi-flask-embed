/*
 * Minimal, XSS-safe Markdown renderer for the administrative Reporting preview.
 *
 * Markdown -> AST -> DOM nodes built with createElement/createTextNode.
 * Raw HTML in the source is never interpreted (it is shown as literal text) and
 * innerHTML is never used, so no sanitization step is needed. Link targets are
 * restricted to http(s) and mailto. This is NOT the final report renderer.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraMarkdown = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SAFE_URL = /^(https?:\/\/|mailto:)/i;
  const ESCAPABLE = '\\`*_{}[]()#+-.!|~>';

  // ---- inline -------------------------------------------------------------
  const INLINE_RULES = [
    ['escape', /\\([\\`*_{}\[\]()#+\-.!|~>])/],
    ['code', /`([^`\n]+)`/],
    ['link', /\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/],
    ['strong', /\*\*([^\s*](?:[\s\S]*?[^\s*])?)\*\*/],
    ['strong', /__([^\s_](?:[\s\S]*?[^\s_])?)__/],
    ['del', /~~([^\s~](?:[\s\S]*?[^\s~])?)~~/],
    ['em', /\*([^\s*](?:[^*]*?[^\s*])?)\*/],
    ['em', /(?:^|(?<=[^\w]))_([^\s_](?:[^_]*?[^\s_])?)_(?!\w)/],
  ];

  function parseInline(text, depth) {
    const nodes = [];
    let rest = String(text);
    depth = depth || 0;
    while (rest.length) {
      let best = null;
      for (const [type, re] of INLINE_RULES) {
        const match = re.exec(rest);
        if (match && (best === null || match.index < best.match.index)) best = { type, match };
      }
      if (!best) { nodes.push({ t: 'text', v: rest }); break; }
      const { type, match } = best;
      if (match.index > 0) nodes.push({ t: 'text', v: rest.slice(0, match.index) });
      if (type === 'escape') nodes.push({ t: 'text', v: match[1] });
      else if (type === 'code') nodes.push({ t: 'code', v: match[1] });
      else if (type === 'link') {
        const children = depth < 4 ? parseInline(match[1], depth + 1) : [{ t: 'text', v: match[1] }];
        if (SAFE_URL.test(match[2])) nodes.push({ t: 'link', href: match[2], c: children });
        else nodes.push({ t: 'text', v: match[0] });
      } else {
        nodes.push({ t: type, c: depth < 4 ? parseInline(match[1], depth + 1) : [{ t: 'text', v: match[1] }] });
      }
      rest = rest.slice(match.index + match[0].length);
    }
    return nodes;
  }

  // Single newlines inside a paragraph/cell/list item become <br>.
  function parseInlineLines(lines) {
    const out = [];
    lines.forEach((line, index) => {
      if (index) out.push({ t: 'br' });
      out.push(...parseInline(line.trim()));
    });
    return out;
  }

  // ---- blocks -------------------------------------------------------------
  const FENCE = /^\s*```/;
  const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
  const HR = /^\s*([-*_])(\s*\1){2,}\s*$/;
  const QUOTE = /^\s*>\s?/;
  const ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

  function splitRow(line) {
    let row = line.trim();
    if (row.startsWith('|')) row = row.slice(1);
    if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1);
    return row.split(/(?<!\\)\|/).map(cell => cell.replace(/\\\|/g, '|').trim());
  }

  function isBlockStart(line, next) {
    return FENCE.test(line) || HEADING.test(line) || HR.test(line) || QUOTE.test(line) || ITEM.test(line)
      || (line.includes('|') && next !== undefined && TABLE_SEP.test(next) && next.includes('-'));
  }

  function indentOf(text) { return text.replace(/\t/g, '    ').length - text.replace(/\t/g, '    ').trimStart().length; }

  function parseList(lines, start) {
    const root = { t: 'list', ordered: /^\d/.test(ITEM.exec(lines[start])[2]), items: [] };
    const stack = [{ indent: indentOf(lines[start]), list: root }];
    let i = start;
    while (i < lines.length) {
      const line = lines[i];
      const match = ITEM.exec(line);
      if (!match) {
        // A blank line only continues the list when another item follows.
        if (!line.trim() && i + 1 < lines.length && ITEM.test(lines[i + 1])) { i += 1; continue; }
        // Indented continuation text belongs to the last item.
        if (line.trim() && indentOf(line) > 0 && !isBlockStart(line, lines[i + 1])) {
          const last = stack[stack.length - 1].list.items.slice(-1)[0];
          if (last) { last.lines.push(line.trim()); i += 1; continue; }
        }
        break;
      }
      const indent = indentOf(line);
      // A different marker kind at the top level starts a new list.
      if (stack.length === 1 && indent <= stack[0].indent && /^\d/.test(match[2]) !== root.ordered) break;
      while (stack.length > 1 && indent < stack[stack.length - 1].indent) stack.pop();
      let top = stack[stack.length - 1];
      if (indent > top.indent && top.list.items.length) {
        const parent = top.list.items[top.list.items.length - 1];
        const sub = { t: 'list', ordered: /^\d/.test(match[2]), items: [] };
        parent.children.push(sub);
        top = { indent, list: sub };
        stack.push(top);
      }
      top.list.items.push({ lines: [match[3]], children: [] });
      i += 1;
    }
    return { block: root, next: i };
  }

  function parseBlocks(lines) {
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      if (FENCE.test(line)) {
        const body = [];
        i += 1;
        while (i < lines.length && !FENCE.test(lines[i])) { body.push(lines[i]); i += 1; }
        i += 1;
        blocks.push({ t: 'pre', v: body.join('\n') });
        continue;
      }
      const heading = HEADING.exec(line);
      if (heading) { blocks.push({ t: 'heading', level: heading[1].length, c: parseInline(heading[2]) }); i += 1; continue; }
      if (HR.test(line)) { blocks.push({ t: 'hr' }); i += 1; continue; }
      if (QUOTE.test(line)) {
        const body = [];
        while (i < lines.length && QUOTE.test(lines[i])) { body.push(lines[i].replace(QUOTE, '')); i += 1; }
        blocks.push({ t: 'quote', c: parseBlocks(body) });
        continue;
      }
      if (line.includes('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes('-')) {
        const head = splitRow(line);
        const align = splitRow(lines[i + 1]).map(cell => (cell.startsWith(':') && cell.endsWith(':') ? 'center'
          : cell.endsWith(':') ? 'right' : cell.startsWith(':') ? 'left' : null));
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes('|')) { rows.push(splitRow(lines[i])); i += 1; }
        const cell = (text, index) => ({ align: align[index] || null, c: parseInline(text) });
        blocks.push({ t: 'table', head: head.map(cell), rows: rows.map(row => head.map((_, index) => cell(row[index] || '', index))) });
        continue;
      }
      if (ITEM.test(line)) {
        const { block, next } = parseList(lines, i);
        blocks.push(block);
        i = next;
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && (para.length === 0 || !isBlockStart(lines[i], lines[i + 1]))) {
        para.push(lines[i]);
        i += 1;
      }
      blocks.push({ t: 'p', c: parseInlineLines(para) });
    }
    return blocks;
  }

  function parse(markdown) {
    return parseBlocks(String(markdown == null ? '' : markdown).replace(/\r\n?/g, '\n').split('\n'));
  }

  // ---- DOM ----------------------------------------------------------------
  function el(doc, tag, className) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function inlineToDom(doc, nodes, parent) {
    for (const node of nodes) {
      if (node.t === 'text') parent.appendChild(doc.createTextNode(node.v));
      else if (node.t === 'br') parent.appendChild(el(doc, 'br'));
      else if (node.t === 'code') { const c = el(doc, 'code'); c.textContent = node.v; parent.appendChild(c); }
      else if (node.t === 'link') {
        const a = el(doc, 'a');
        a.setAttribute('href', node.href);
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer nofollow');
        inlineToDom(doc, node.c, a);
        parent.appendChild(a);
      } else {
        const tag = { strong: 'strong', em: 'em', del: 'del' }[node.t];
        const child = el(doc, tag);
        inlineToDom(doc, node.c, child);
        parent.appendChild(child);
      }
    }
  }

  function listToDom(doc, list) {
    const node = el(doc, list.ordered ? 'ol' : 'ul');
    for (const item of list.items) {
      const li = el(doc, 'li');
      inlineToDom(doc, parseInlineLines(item.lines), li);
      for (const child of item.children) li.appendChild(listToDom(doc, child));
      node.appendChild(li);
    }
    return node;
  }

  function blockToDom(doc, block) {
    switch (block.t) {
      case 'heading': { const h = el(doc, 'h' + block.level); inlineToDom(doc, block.c, h); return h; }
      case 'p': { const p = el(doc, 'p'); inlineToDom(doc, block.c, p); return p; }
      case 'hr': return el(doc, 'hr');
      case 'pre': { const pre = el(doc, 'pre'); const code = el(doc, 'code'); code.textContent = block.v; pre.appendChild(code); return pre; }
      case 'quote': { const q = el(doc, 'blockquote'); block.c.forEach(child => q.appendChild(blockToDom(doc, child))); return q; }
      case 'list': return listToDom(doc, block);
      case 'table': {
        const wrap = el(doc, 'div', 'table-responsive');
        const table = el(doc, 'table', 'table table-sm table-bordered w-auto');
        const thead = el(doc, 'thead'); const headRow = el(doc, 'tr');
        for (const cell of block.head) {
          const th = el(doc, 'th'); if (cell.align) th.style.textAlign = cell.align;
          inlineToDom(doc, cell.c, th); headRow.appendChild(th);
        }
        thead.appendChild(headRow); table.appendChild(thead);
        const tbody = el(doc, 'tbody');
        for (const row of block.rows) {
          const tr = el(doc, 'tr');
          for (const cell of row) {
            const td = el(doc, 'td'); if (cell.align) td.style.textAlign = cell.align;
            inlineToDom(doc, cell.c, td); tr.appendChild(td);
          }
          tbody.appendChild(tr);
        }
        table.appendChild(tbody); wrap.appendChild(table);
        return wrap;
      }
      default: return doc.createTextNode('');
    }
  }

  /** Render Markdown into a DocumentFragment (or any node built by `doc`). */
  function render(markdown, doc) {
    doc = doc || document;
    const fragment = doc.createDocumentFragment();
    for (const block of parse(markdown)) fragment.appendChild(blockToDom(doc, block));
    return fragment;
  }

  return { parse, render, parseInline };
});
