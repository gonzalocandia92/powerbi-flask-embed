const KM = require('../../app/static/js/klara_markdown.js');
class N { constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.style={};this.className='';this._text=null;}
  appendChild(c){this.children.push(c);return c;} setAttribute(k,v){this.attrs[k]=v;}
  set textContent(v){this._text=v;} }
const doc = { createElement:t=>new N(t), createTextNode:v=>({tag:'#text',v}), createDocumentFragment:()=>new N('#frag') };
const ser = n => n.tag==='#text'? n.v.replace(/</g,'&lt;') : (n._text!==null? `<${n.tag}>${n._text.replace(/</g,'&lt;')}</${n.tag}>` :
  `<${n.tag}${n.attrs.href?` href="${n.attrs.href}"`:''}${n.style.textAlign?` align=${n.style.textAlign}`:''}>${n.children.map(ser).join('')}</${n.tag}>`);
const r = md => KM.render(md, doc).children.map(ser).join('');
const assert = require('assert');
const md = `# Título\n\nHola **negrita** y *cursiva* con \`code\`.\nSegunda línea\n\n- uno\n- dos\n  - sub\n- tres\n\n1. a\n2. b\n\n| Suc | Ventas |\n|:--|--:|\n| Centro | $10 |\n| Norte | $5 |\n\n> cita\n\n\`\`\`\n<b>x</b>\n\`\`\``;
const out = r(md); console.log(out);
assert(out.includes('<h1>Título</h1>')); assert(out.includes('<strong>negrita</strong>')); assert(out.includes('<em>cursiva</em>'));
assert(out.includes('<br>')); assert(out.includes('<ul><li>uno</li><li>dos<ul><li>sub</li></ul></li><li>tres</li></ul>'));
assert(out.includes('<ol><li>a</li><li>b</li></ol>')); assert(out.includes('<th align=left>Suc</th>')); assert(out.includes('<td align=right>$10</td>'));
assert(out.includes('<blockquote><p>cita</p></blockquote>')); assert(out.includes('<pre><code>&lt;b>x&lt;/b></code></pre>'));
// XSS: raw HTML stays text, unsafe URLs are not links
const evil = r('<img src=x onerror=alert(1)> <script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://a.com) [d](data:text/html,1)');
console.log(evil);
assert(!/<img|<script/.test(evil.replace(/&lt;/g,''))); assert(!evil.includes('href="javascript')); assert(!evil.includes('href="data')); assert(evil.includes('href="https://a.com"'));
assert(r('_a_ snake_case_word 2*3*4') .includes('<em>a</em>')); assert(r('snake_case_word').includes('snake_case_word') && !r('snake_case_word').includes('<em>'));
assert.strictEqual(r(''), ''); assert.strictEqual(r(null), '');
console.log('OK');
