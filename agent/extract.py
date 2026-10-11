"""
Turn the page a worker is on into compact text for the model, with refs for everything it can act on.

What a person would see, not the raw DOM: hidden elements are dropped (decoy text and injected instructions are
usually `hidden` or `aria-hidden`), collapsed `<details>` are opened, tooltips and canvas/img labels are inlined,
same-origin iframes are walked in place and zero-width characters are stripped. Interactive elements get a
`data-ar` ref such as `e12` that `locate` resolves again for clicking and filling.
"""
import asyncio
import difflib
import re

from playwright.async_api import Frame, Locator, Page

# A page whose script hangs (an endless loop) never answers an evaluate: every call into a page is bounded, so a
# frozen tab costs seconds, not the run.
WALK_SECONDS = 20.0
FRAME_SECONDS = 5.0
DIFF_CHARS = 40000  # longer views are sent whole instead of diffed (difflib is quadratic in the worst case)

WALK_JS = r"""
([start, stamp]) => {
  const ZW = /[​‌‍⁠﻿­]/g;
  const clean = s => (s || '').replace(ZW, '').replace(/\s+/g, ' ').trim();
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK']);
  const BLOCK = /^(block|flex|grid|table|table-row|list-item|flow-root|table-caption)$/;
  const ROLES = new Set(['button', 'link', 'tab', 'menuitem', 'option', 'checkbox', 'radio', 'switch', 'combobox',
                         'slider', 'textbox', 'searchbox']);
  const SELECT_CHARS = 600, SELECT_HEAD = 8, SELECT_TAIL = 2;  // longer option lists are shortened
  let n = start;
  const out = [];
  const crossOrigin = [];
  const emit = s => out.push(s);

  function isHidden(el, win) {
    if (el.hidden || el.getAttribute('aria-hidden') === 'true' || el.inert) return true;
    const cs = win.getComputedStyle(el);
    return cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse';
  }
  function textOnly(node) {  // a label's own words, without the controls and styles it wraps
    const copy = node.cloneNode(true);
    copy.querySelectorAll('select,option,input,textarea,button,style,script').forEach(e => e.remove());
    return copy.textContent;
  }
  function labelOf(el) {
    const own = el.getAttribute('aria-label');
    if (own) return clean(own);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(id => el.ownerDocument.getElementById(id)).filter(Boolean)
        .map(e => e.textContent).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.labels && el.labels.length) {
      const t = clean(Array.from(el.labels).map(textOnly).join(' '));
      if (t) return t;
    }
    return clean(el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('name') || '');
  }
  function isInteractive(el) {
    const tag = el.tagName;
    if (tag === 'A' && el.hasAttribute('href')) return true;
    if (tag === 'BUTTON' || tag === 'SELECT' || tag === 'TEXTAREA') return true;
    if (tag === 'SUMMARY') return el.hasAttribute('onclick');  // details are always shown open
    if (tag === 'INPUT') return el.type !== 'hidden';
    const role = el.getAttribute('role');
    return !!(role && ROLES.has(role)) || el.hasAttribute('onclick');
  }
  function ref(el) { const r = 'e' + (n++); el.setAttribute('data-ar', r); return r; }

  function describe(el) {
    const r = ref(el);
    const tag = el.tagName;
    const dis = el.disabled ? ' disabled' : '';
    if (tag === 'A') {
      const name = clean(el.innerText) || labelOf(el);
      return `[${r} link "${name}" -> ${el.getAttribute('href')}]`;
    }
    if (tag === 'SELECT') {
      const opts = Array.from(el.options);
      const sel = opts.filter(o => o.selected).map(o => clean(o.textContent)).join(', ');
      const names = opts.map(o => clean(o.textContent) + (o.disabled ? ' (disabled)' : ''));
      let list = names.join(' | ');
      if (list.length > SELECT_CHARS && names.length > SELECT_HEAD + SELECT_TAIL + 1) {
        // a long list costs tokens on every view; show its shape, options(ref, contains) lists the rest
        const hidden = names.length - SELECT_HEAD - SELECT_TAIL;
        list = [...names.slice(0, SELECT_HEAD), `... ${hidden} more: options(${r}) ...`,
                ...names.slice(-SELECT_TAIL)].join(' | ');
        list = `(${names.length}) ${list}`;
      }
      return `[${r} select "${labelOf(el)}" selected="${sel}"${dis} options: ${list}]`;
    }
    if (tag === 'TEXTAREA') return `[${r} textarea "${labelOf(el)}" value="${clean(el.value)}"${dis}]`;
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio')
        return `[${r} ${t} "${labelOf(el)}"${el.checked ? ' checked' : ' unchecked'}${dis}]`;
      if (t === 'range')
        return `[${r} slider "${labelOf(el)}" value=${el.value} min=${el.min || 0} max=${el.max || 100}${dis}]`;
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image')
        return `[${r} button "${clean(el.value) || labelOf(el)}"${dis}]`;
      const kind = t === 'password' ? 'password' : el.getAttribute('role') === 'combobox' ? 'combobox' : 'textbox';
      const value = t === 'password' ? (el.value ? '***' : '') : clean(el.value);
      const ph = el.placeholder ? ` placeholder="${clean(el.placeholder)}"` : '';
      return `[${r} ${kind} "${labelOf(el)}" value="${value}"${ph}${el.required ? ' required' : ''}${dis}]`;
    }
    const role = el.getAttribute('role') || tag.toLowerCase();
    if (role === 'combobox') {  // a custom dropdown: its label, and the choice it shows as its value
      const label = labelOf(el);
      if (label) {
        const state = el.getAttribute('aria-expanded') ? ` expanded=${el.getAttribute('aria-expanded')}` : '';
        const off = dis || (el.getAttribute('aria-disabled') === 'true' ? ' disabled' : '');
        return `[${r} combobox "${label}" value="${clean(el.innerText)}"${state}${off}]`;
      }
    }
    const name = clean(el.innerText) || labelOf(el);
    const state = el.getAttribute('aria-expanded') ? ` expanded=${el.getAttribute('aria-expanded')}` : '';
    return `[${r} ${role} "${name}"${state}${dis}]`;
  }

  function groupOf(el) {  // a radio group (fieldset or role) or a listbox as one choice control, else ''
    const role = el.getAttribute('role');
    let items, label, on;
    if (role === 'listbox') {
      items = Array.from(el.querySelectorAll('[role=option]'));
      label = labelOf(el);
      on = o => o.getAttribute('aria-selected') === 'true';
    } else if (role === 'radiogroup' || el.tagName === 'FIELDSET') {
      items = Array.from(el.querySelectorAll('input[type=radio], [role=radio]'));
      const legend = el.tagName === 'FIELDSET' && Array.from(el.children).find(c => c.tagName === 'LEGEND');
      label = legend ? clean(textOnly(legend)) : labelOf(el);
      on = o => o.checked === true || o.getAttribute('aria-checked') === 'true';
    } else return '';
    if (!items.length || (el.tagName === 'FIELDSET' && role !== 'radiogroup' &&
                          el.querySelectorAll('input:not([type=radio]):not([type=hidden]),select,textarea').length))
      return '';
    const text = o => (o.tagName === 'INPUT' ? labelOf(o) : clean(o.innerText) || labelOf(o));
    const r = ref(el);
    const sel = items.filter(on).map(text).join(', ');
    const off = el.disabled || el.getAttribute('aria-disabled') === 'true' ? ' disabled' : '';
    const kind = role === 'listbox' ? 'listbox' : 'radiogroup';
    return `[${r} ${kind} "${label}" selected="${sel}"${off} options: ${items.map(text).join(' | ')}]`;
  }

  function walkDoc(doc, win) {
    doc.querySelectorAll('[data-ar]').forEach(e => e.removeAttribute('data-ar'));
    doc.querySelectorAll('details:not([open])').forEach(d => { d.open = true; });
    doc.documentElement.dataset.arWalked = stamp;
    if (doc.body) walk(doc.body, win);
  }

  function walk(node, win) {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        const raw = child.nodeValue.replace(ZW, '');
        const pre = /^pre/.test(win.getComputedStyle(node).whiteSpace);  // keep the lines of files and tables
        const t = pre ? raw.replace(/[ \t]+/g, ' ').replace(/\n/g, '\u2028') : raw.replace(/\s+/g, ' ');
        if (t.trim()) emit(t);
        continue;
      }
      if (child.nodeType !== 1) continue;
      const el = child;
      if (SKIP.has(el.tagName)) continue;
      const role = el.getAttribute('role');
      if (role === 'tooltip') {
        if (!el.hidden && el.getAttribute('aria-hidden') !== 'true') emit(` (tooltip: ${clean(el.textContent)}) `);
        continue;
      }
      if (isHidden(el, win)) continue;
      const tag = el.tagName;
      if (tag === 'CANVAS' || tag === 'IMG' || tag === 'svg' || tag === 'SVG' || role === 'img') {
        const label = clean(el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title'));
        if (label) emit(` [img: ${label}] `);
        continue;
      }
      if (tag === 'IFRAME' || tag === 'FRAME') {
        const src = el.getAttribute('src') || '';
        let inner = null;
        try { inner = el.contentDocument; } catch (e) { inner = null; }
        if (inner && inner.body) {
          emit(`\n[frame ${src}]\n`);
          walkDoc(inner, el.contentWindow);
          emit(`\n[/frame]\n`);
        } else {
          crossOrigin.push(src);
          emit(`\n[frame ${src} (separate)]\n`);
        }
        continue;
      }
      const cs = win.getComputedStyle(el);
      const block = BLOCK.test(cs.display) || /^H[1-6]$/.test(tag) || tag === 'BR';
      if (block) emit('\n');
      if (/^H[1-6]$/.test(tag)) emit('#'.repeat(+tag[1]) + ' ');
      if (cs.display === 'list-item') emit('- ');
      const group = groupOf(el);
      if (group) {
        emit(' ' + group + ' ');
        walk(el, win);
      } else if (isInteractive(el)) {
        emit(' ' + describe(el) + ' ');
        // a label or summary may wrap further controls; walk those, not the text again
        el.querySelectorAll('input,select,textarea,button').forEach(inner => {
          if (inner !== el && !isHidden(inner, win)) emit(' ' + describe(inner) + ' ');
        });
      } else {
        walk(el, win);
        if (tag === 'TIME' && el.getAttribute('datetime')) emit(` (datetime=${el.getAttribute('datetime')})`);
        const title = el.getAttribute('title');
        if (title && !clean(el.textContent).includes(clean(title))) emit(` (title: ${clean(title)})`);
      }
      if (cs.display === 'table-cell') emit(' | ');
      if (block) emit('\n');
    }
  }

  walkDoc(document, window);
  const text = out.join('')
    .replace(/[ \t]+/g, ' ')
    .replace(/ *\n */g, '\n')
    .replace(/\n{2,}/g, '\n')
    .replace(/\u2028/g, '\n')
    .trim();
  return {text, next: n, crossOrigin};
}
"""


async def observe(page: Page, stamp: str = "1") -> str:
    """The page's visible content as text with `[eN ...]` refs; frames the main walk could not enter follow."""
    result = await asyncio.wait_for(page.main_frame.evaluate(WALK_JS, [1, stamp]), WALK_SECONDS)
    parts = [result["text"]]
    next_id = result["next"]
    if result["crossOrigin"]:
        for frame in page.frames[1:]:
            try:
                walked = await asyncio.wait_for(
                    frame.evaluate("document.documentElement.dataset.arWalked || ''"), FRAME_SECONDS)
                if walked == stamp:
                    continue
                sub = await asyncio.wait_for(frame.evaluate(WALK_JS, [next_id, stamp]), WALK_SECONDS)
            except Exception:
                continue
            next_id = sub["next"]
            parts.append(f"[frame {frame.url}]\n{sub['text']}\n[/frame]")
    return "\n".join(parts)


async def locate(page: Page, ref: str) -> Locator | None:
    """The element a ref from the latest `observe` points at, in whichever frame it lives."""
    selector = f'[data-ar="{ref.strip()}"]'
    frames: list[Frame] = [page.main_frame, *page.frames[1:]]
    for frame in frames:
        try:
            locator = frame.locator(selector)
            if await asyncio.wait_for(locator.count(), FRAME_SECONDS):
                return locator.first
        except Exception:
            continue
    return None


OPTIONS_JS = r"""
(e, contains) => {
  const ZW = /[​‌‍⁠﻿­]/g;
  const clean = s => (s || '').replace(ZW, '').replace(/\s+/g, ' ').trim();
  const want = clean(contains).toLowerCase();
  const all = Array.from(e.options || []).map(o => clean(o.textContent) + (o.disabled ? ' (disabled)' : '')
                                                  + (o.selected ? ' (selected)' : ''));
  return {total: all.length, hits: all.filter(t => t.toLowerCase().includes(want))};
}
"""


async def select_options(page: Page, ref: str, contains: str = "") -> tuple[int, list[str]] | None:
    """All options of the select `ref` (or those whose text contains `contains`, ignoring case); None without one."""
    element = await locate(page, ref)
    if element is None or await asyncio.wait_for(element.evaluate("e => e.tagName"), FRAME_SECONDS) != "SELECT":
        return None
    found = await asyncio.wait_for(element.evaluate(OPTIONS_JS, contains), WALK_SECONDS)
    return found["total"], found["hits"]



CONTROL = re.compile(r"(\[e\d+ (?:[^\[\]]|\[[^\[\]]*\])*\])")  # one [eN ...] element, allowing [..] inside


def _pieces(view: str) -> list[str]:
    """A view cut into lines, and lines into their text and each control, so a changed field is one piece."""
    return [piece.strip() for line in view.split("\n") for piece in CONTROL.split(line) if piece.strip()]


def changes(old: str, new: str, share: float = 0.4) -> str | None:
    """The pieces (text runs and controls) of `new` that differ from `old`, each after the last unchanged one.

    Unchanged pieces are identical text, refs included, so together with the old view they give the new page.
    None when the change is a large part of the page (then the whole view is clearer and not much longer).
    """
    if len(old) + len(new) > DIFF_CHARS:
        return None
    before, after = _pieces(old), _pieces(new)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        out.append(f"@@ after {before[i1 - 1][:80]!r}:" if i1 else "@@ at the top:")
        out += [f"- {piece[:80]}" + ("..." if len(piece) > 80 else "") for piece in before[i1:i2]]
        out += [f"+ {piece}" for piece in after[j1:j2]]
    text = "\n".join(out)
    return text if len(text) <= share * len(new) else None
