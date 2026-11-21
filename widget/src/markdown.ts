/**
 * The assistant's answers as the reader expects them (Mike, 21 Nov 2026: "- Series" and **bold** showed as typed):
 * paragraphs, bullet and numbered lists (one level of nesting), headings shown as bold lines, **bold**, *italic*,
 * `code` and [links](https://…). Built with DOM nodes and textContent only, never HTML from the text, so nothing the
 * model or the app says can run in the page. Anything else stays as written.
 */

type Inline = (Node | string)[];

const BULLET = /^(\s*)[-*•]\s+(.*)$/;
const NUMBER = /^(\s*)(\d{1,3})[.)]\s+(.*)$/;
const HEADING = /^#{1,6}\s+(.*)$/;
// **bold**, *italic* or _italic_, `code`, [text](url): the first match wins, left to right.
const INLINE = /\*\*([^*]+?)\*\*|`([^`]+?)`|\[([^\]]+?)\]\(([^)\s]+?)\)|(?<![\w*])\*([^*\s][^*]*?)\*(?!\w)|(?<!\w)_([^_\s][^_]*?)_(?!\w)/g;

function safeHref(url: string): string | null {
  try {
    const u = new URL(url, typeof location === "undefined" ? "https://example.invalid/" : location.href);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, kids: Inline = []): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  for (const k of kids) e.append(k);
  return e;
}

/** One line's inline marks as nodes. */
export function inline(text: string): Inline {
  const out: Inline = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const [whole, bold, code, linkText, linkUrl, star, under] = m;
    if (bold !== undefined) out.push(el("strong", inline(bold)));
    else if (code !== undefined) out.push(el("code", [code]));
    else if (linkText !== undefined) {
      const href = safeHref(linkUrl!);
      if (href) {
        const a = el("a", inline(linkText));
        a.href = href;
        a.target = "_blank";
        a.rel = "noreferrer";
        out.push(a);
      } else out.push(whole);
    } else out.push(el("em", inline((star ?? under)!)));
    last = at + whole.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

interface Item {
  ordered: boolean;
  depth: number;
  start: number;
  text: string;
}

function listItem(line: string): Item | null {
  const b = BULLET.exec(line);
  if (b) return { ordered: false, depth: b[1].length >= 2 ? 1 : 0, start: 1, text: b[2] };
  const n = NUMBER.exec(line);
  if (n) return { ordered: true, depth: n[1].length >= 2 ? 1 : 0, start: Number(n[2]), text: n[3] };
  return null;
}

/** A run of list lines as one list, items indented two spaces or more nested under the item before them. */
function renderList(items: Item[]): HTMLElement {
  const top = items[0];
  const list = el(top.ordered ? "ol" : "ul");
  if (top.ordered && top.start !== 1) list.setAttribute("start", String(top.start));
  let li: HTMLLIElement | null = null;
  let nested: HTMLElement | null = null;
  for (const it of items) {
    if (it.depth > 0 && li) {
      if (!nested) {
        nested = el(it.ordered ? "ol" : "ul");
        li.append(nested);
      }
      nested.append(el("li", inline(it.text)));
      continue;
    }
    li = el("li", inline(it.text));
    nested = null;
    list.append(li);
  }
  return list;
}

/** The text as blocks: paragraphs (a single line break kept), lists, and headings as bold lines. */
export function renderMarkdown(text: string): DocumentFragment {
  const out = document.createDocumentFragment();
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let para: string[] = [];
  let items: Item[] = [];
  const flushPara = () => {
    if (!para.length) return;
    const p = el("p");
    para.forEach((line, i) => {
      if (i) p.append(el("br"));
      for (const node of inline(line)) p.append(node);
    });
    out.append(p);
    para = [];
  };
  const flushList = () => {
    if (!items.length) return;
    out.append(renderList(items));
    items = [];
  };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    const item = listItem(line);
    if (item && (item.depth === 0 || items.length)) {
      flushPara();
      items.push(item);
      continue;
    }
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    flushList();
    const heading = HEADING.exec(line);
    if (heading) {
      flushPara();
      out.append(el("p", [el("strong", inline(heading[1]))]));
      continue;
    }
    para.push(line.trim());
  }
  flushPara();
  flushList();
  return out;
}
