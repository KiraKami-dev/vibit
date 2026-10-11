"""
Fill one bid position in a web form from calculated values: the mechanical part of the bid worker.

The model reads the form, says which position is on screen and which control takes which value (by the control's
label as its view shows it); `fill_position` then sets the controls in that order. Choices that narrow each other
(select one level, confirm, the next level's options appear) get the confirm button the model named after every
level. Options are matched by value, never by their place in the list, so text-sorted lists ("10, 125, 14, 8") and
look-alike neighbours ("63", "64") do not matter. Nothing here knows a site, a label or a value.

Forms differ: native selects, ARIA comboboxes and listboxes and radio groups are all choices; a page may list
several positions (a table row or a card each), and then every control is taken from the position's own row; the
decimal mark follows the brief (FillPlan.decimal), and a number-only field always takes a decimal point.
"""
import asyncio
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from extract import locate

ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
CONTROL = re.compile(r'\[(e\d+) (select|combobox|listbox|radiogroup|textbox|textarea|button|checkbox|radio) '
                     r'"((?:[^"\\]|\\.)*)"')
FIELD_KINDS = ("select", "combobox", "listbox", "radiogroup", "textbox", "textarea")  # controls that take a value
CHOICE_KINDS = ("select", "combobox", "listbox", "radiogroup")
NUMBER_OPTION = re.compile(r"^\s*([+-]?\d[\d.,]*)\s*[^\d]*$")

# Values of a bid position: the answer fields from calc plus the material's attributes (keys of `attributes`).
MONEY_KEYS = {"unit_price", "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total"}
COUNT_KEYS = {"quantity", "lead_time_weeks"}
REQUIRED_KEYS = ("position", "material_number", "quantity", "unit_price", "lead_time_weeks")
# the material attributes the form's choices lead through (orchestrator.material_attributes gives these names)
ATTRIBUTE_KEYS = ("catalog_group", "bore", "stroke", "pressure", "mounting", "certification")
# every value name a position control can take, known before any value is calculated
VALUE_NAMES = (*REQUIRED_KEYS[1:], "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total",
               *ATTRIBUTE_KEYS)


def clean(text: str) -> str:
    return " ".join(ZERO_WIDTH.sub("", text or "").split()).casefold()


def parse_number(text: str) -> Decimal | None:
    """A number written the German way ("1.234,5"), the English way ("1,234.5") or plain; None if it is none."""
    t = ZERO_WIDTH.sub("", text or "").strip().replace(" ", "").replace("\u202f", "").replace("\u00a0", "")
    if not re.fullmatch(r"[+-]?\d[\d.,]*", t):
        return None
    if "," in t and "." in t:  # the later separator is the decimal one
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:  # one comma is a decimal comma (German), several are thousands separators
        t = t.replace(",", ".") if t.count(",") == 1 else t.replace(",", "")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def german(value, places: int = 2, mark: str = ",") -> str:
    """A number without thousands separators, decimal comma by default: 1234.5 -> "1234,50", places=0 -> "1235"."""
    number = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{number:.{places}f}".replace(".", mark)


def form_value(key: str, value, mark: str = ",") -> str:
    """The text a form field takes for a position value: money with two decimals, counts as integers."""
    if key in MONEY_KEYS:
        return german(value, 2, mark)
    if key in COUNT_KEYS:
        return german(value, 0, mark)
    return str(value)


def number_in(text: str) -> Decimal | None:
    """The number a field shows, also with a unit or currency around it ("2,50 EUR", "3 Wochen"); None without."""
    t = ZERO_WIDTH.sub("", text or "").replace("\u00a0", "").replace("\u202f", "")
    found = re.search(r"[+-]?\d[\d.,]*", t.replace(" ", ""))
    return parse_number(found.group(0).rstrip(".,")) if found else None


# what a brief may say about the number format of the form; explicit words win over example numbers
COMMA_WORDS = re.compile(r"dezimalkomma|decimal comma|komma als dezimal|comma as (?:the )?decimal|"
                         r"deutsche[nms]? (?:zahlen)?format|german (?:number )?format")
POINT_WORDS = re.compile(r"dezimalpunkt|decimal point|decimal dot|punkt als dezimal|point as (?:the )?decimal|"
                         r"dot as (?:the )?decimal|english (?:number )?format")
COMMA_EXAMPLE = re.compile(r"(?<![\d,.])\d+,\d{2}(?!\d|[.,]\d)")
POINT_EXAMPLE = re.compile(r"(?<![\d,.])\d+\.\d{2}(?!\d|[.,]\d)")


def decimal_mark(text: str) -> str:
    """
    The decimal mark the brief's submission steps ask for: "." or ",". Stated words first, then the example
    amounts it writes (only when all of them agree); a decimal comma (the German default) when it says nothing.
    """
    t = clean(text)
    comma, point = bool(COMMA_WORDS.search(t)), bool(POINT_WORDS.search(t))
    if comma != point:
        return "," if comma else "."
    comma, point = bool(COMMA_EXAMPLE.search(t)), bool(POINT_EXAMPLE.search(t))
    return "." if point and not comma else ","


def same_number(shown: str, wanted: str) -> bool:
    """Whether a field shows the number we entered, whatever its notation ("2.5" for "2,50")."""
    a, b = number_in(shown), number_in(wanted)
    return a is not None and a == b


def match_option(options: list[dict], wanted: str) -> tuple[dict | None, str]:
    """
    The one option of a select that means `wanted`, or None and why.

    Tried in order, each must give exactly one option: the same text or value; the same number (an option that is
    a number plus a unit, "3 Wochen" for 3); `wanted` as a whole token of the option text (a catalogue number
    followed by details). Placeholder options (empty value) never match.
    """
    real = [o for o in options if (o.get("value") or "").strip() and not o.get("disabled")]
    want = clean(wanted)
    if not want:
        return None, "no value to choose"
    exact = [o for o in real if clean(o["text"]) == want or clean(o["value"]) == want]
    if len(exact) == 1:
        return exact[0], ""
    number = parse_number(wanted.split()[0]) if wanted.split() else None
    if number is not None and NUMBER_OPTION.match(wanted):
        same = []
        for o in real:
            m = NUMBER_OPTION.match(ZERO_WIDTH.sub("", o["text"]))
            if m and parse_number(m.group(1)) == number:
                same.append(o)
        if len(same) == 1:
            return same[0], ""
        if len(same) > 1:
            return None, f"{len(same)} options equal {wanted!r}"
    token = re.compile(r"(?<![\w-])" + re.escape(want) + r"(?![\w-])")
    hits = [o for o in real if re.search(r"\d", want) and token.search(clean(o["text"]))]
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        return None, f"{len(hits)} options contain {wanted!r}: " + " | ".join(o["text"] for o in hits[:8])
    return None, f"no option means {wanted!r}"


def controls(view: str) -> list[tuple[str, str, str]]:
    """(ref, kind, label) of every form control in a view, in page order."""
    return [(ref, kind, label.replace('\\"', '"')) for ref, kind, label in CONTROL.findall(view)]


def find_control(view: str, label: str, kinds: tuple[str, ...] = ()) -> tuple[str | None, str]:
    """The ref of the control whose label is `label` (exactly, else the one label that contains it)."""
    want = clean(label)
    found = [(ref, kind, text) for ref, kind, text in controls(view) if not kinds or kind in kinds]
    exact = [c for c in found if clean(c[2]) == want]
    if exact:
        return exact[0][0], ""
    partial = [c for c in found if want and want in clean(c[2])]
    if len(partial) == 1:
        return partial[0][0], ""
    if partial:
        return None, f"{label!r} matches several controls: " + ", ".join(repr(c[2]) for c in partial[:6])
    return None, f"no control labelled {label!r} on the page"


def mapping_problems(view: str, fields: dict[str, str], known: set[str], buttons: tuple[str, ...] = ()) -> list[str]:
    """
    What is wrong with a control mapping (label -> value name) for the form in `view`: unknown value names, labels
    that name no control (or several), required values left unmapped, buttons not on the page. [] when usable.
    """
    problems = []
    unknown = sorted({name for name in fields.values() if name not in known})
    if unknown:
        problems.append(f"unknown value names {unknown}; use only: {', '.join(sorted(known))}")
    for label in fields:
        ref, why = find_control(view, label, FIELD_KINDS)
        if ref is None:
            problems.append(why)
    unmapped = sorted(set(REQUIRED_KEYS[1:]) - set(fields.values()))
    if unmapped:
        problems.append(f"no control mapped for {', '.join(unmapped)}")
    for button in filter(None, buttons):
        ref, why = find_control(view, button, ("button",))
        if ref is None:
            problems.append(why)
    return problems


ELEMENT = re.compile(r"\[e\d+ (?:[^\[\]]|\[[^\[\]]*\])*\]")  # one [eN ...] element of a view
FIELD_LINE = re.compile(r"\[e\d+ (?:" + "|".join(FIELD_KINDS) + ") ")


def position_on_page(view: str, position: str) -> bool:
    """
    Whether the page is about this position: its number, as a whole token (01.04 is not 01.041 or 101.04), in a
    heading, in the text above the first choice or field (not among the options further down), or as the text of
    a line that holds fields of its own (a table row or card per position; not a list of positions elsewhere).
    """
    view = ZERO_WIDTH.sub("", view)
    first = re.search(r"\[e\d+ (select|combobox|listbox|radiogroup|textarea|checkbox|radio) ", view)
    head = view[:first.start()] if first else view
    head += "\n" + "\n".join(line for line in view.splitlines() if line.startswith("#"))
    head += "\n" + "\n".join(ELEMENT.sub(" ", line) for line in view.splitlines() if FIELD_LINE.search(line))
    return re.search(r"(?<![\w.])" + re.escape(position.strip()) + r"(?![\w]|\.\d)", head) is not None


def unmapped(view: str, entry: dict, fields: dict[str, str]) -> tuple[list[str], list[str]]:
    """
    Answer values of `entry` that no control is mapped to, and the labels of the page's fields (text boxes, lists)
    that no mapping names. Both non-empty means the model probably skipped part of the form (a live run left every
    price breakdown field empty that way).
    """
    mapped = {clean(label) for label in fields}
    names = set(fields.values())
    values = sorted(k for k in entry if k in MONEY_KEYS | COUNT_KEYS | {"material_number"} and k not in names)
    labels = [label for _, kind, label in controls(view)
              if kind in FIELD_KINDS and clean(label) not in mapped]
    return values, labels


def resolve_position(given: str, numbers, view: str) -> tuple[str | None, list[str]]:
    """
    Which of the bid's position `numbers` to enter now, and the bid's positions the page shows. The page decides:
    forms show positions in their own order, and a model that expects the bid's order names the next position of
    its list while the page shows another one (live runs omitted correct positions that way). So: the position
    `given` names (also inside a longer label such as "Position 01.04") when the page shows it, else the one bid
    position the page shows, else `given` itself if it is a bid position (the caller reports it is not on the page),
    else None (the page shows none of the bid's positions, or several).
    """
    numbers = [str(n).strip() for n in numbers]
    shown = [n for n in numbers if position_on_page(view, n)]
    given = ZERO_WIDTH.sub("", given or "").strip()
    named = [given] if given in numbers else [
        n for n in numbers if re.search(r"(?<![\w.])" + re.escape(n) + r"(?![\w]|\.\d)", given)]
    if len(named) == 1 and named[0] in shown:
        return named[0], shown
    if len(shown) == 1:
        return shown[0], shown
    if len(named) == 1 and not shown:
        return named[0], shown
    return None, shown


def complete_positions(answer: dict, attributes: dict[str, dict[str, str]] | None = None) -> dict[str, dict]:
    """
    The positions that can be entered, by position number: every required answer field present and, for the
    choices that lead to the material, the material's attributes. Others are left out (omitted in the form).
    """
    if not answer or answer.get("decision") != "bid":
        return {}
    attributes = attributes or {}
    entries = {}
    for pos in answer.get("positions", []):
        if any(pos.get(k) in (None, "") for k in REQUIRED_KEYS):
            continue
        entry = {k: v for k, v in pos.items() if v is not None}
        for key, value in (attributes.get(str(pos["position"])) or {}).items():
            entry.setdefault(key, value)
        entries[str(pos["position"]).strip()] = entry
    return entries


@dataclass
class FillPlan:
    """
    What the model said about the form: control label -> value key, the button that confirms a choice, and the
    decimal mark the brief asks for (number-only fields take a decimal point whatever it says).
    """

    fields: dict[str, str] = field(default_factory=dict)
    confirm: str = ""
    decimal: str = ","


NUMBER_ONLY = "A number must match exactly: leave this position out."  # a value failure, not a form problem

KIND_JS = """e => {
  const role = e.getAttribute('role');
  if (e.tagName === 'SELECT') return 'select';
  if (role === 'listbox') return 'listbox';
  if (role === 'radiogroup' || e.tagName === 'FIELDSET') return 'radiogroup';
  if (role === 'combobox') return e.tagName === 'INPUT' ? 'typeahead' : 'combobox';
  if (e.tagName === 'INPUT' && (e.type || '').toLowerCase() === 'number') return 'number';
  return 'text';
}"""
DISABLED_JS = "e => !!(e.disabled || e.readOnly === true || e.getAttribute('aria-disabled') === 'true')"
SHOWN_JS = "e => ('value' in e && typeof e.value === 'string') ? e.value : (e.innerText || '')"

# The options of a choice that is not a native select (radio group, listbox, opened combobox), each tagged with a
# ref of its own so it can be clicked like a person would.
OPTIONS_JS = r"""(e, base) => {
  const clean = s => (s || '').replace(/[​‌‍⁠﻿­]/g, '').replace(/\s+/g, ' ').trim();
  const doc = e.ownerDocument, win = doc.defaultView;
  const shown = o => {
    if (o.hidden || o.getAttribute('aria-hidden') === 'true') return false;
    const cs = win.getComputedStyle(o);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const label = o => {
    if (o.getAttribute('aria-label')) return clean(o.getAttribute('aria-label'));
    if (o.labels && o.labels.length) return clean(Array.from(o.labels).map(l => l.textContent).join(' '));
    return clean(o.innerText || o.textContent || o.value || '');
  };
  const role = e.getAttribute('role');
  let items, on;
  if (role === 'radiogroup' || e.tagName === 'FIELDSET') {
    items = Array.from(e.querySelectorAll('input[type=radio], [role=radio]'));
    on = o => o.checked === true || o.getAttribute('aria-checked') === 'true';
  } else {
    on = o => o.getAttribute('aria-selected') === 'true';
    if (role === 'listbox') items = Array.from(e.querySelectorAll('[role=option]'));
    else {  // a combobox: its popup, else the options of a list that no other control owns
      const ids = (e.getAttribute('aria-controls') || e.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean);
      const pops = ids.map(id => doc.getElementById(id)).filter(Boolean);
      items = pops.flatMap(p => Array.from(p.querySelectorAll('[role=option]')));
      if (!items.length) {
        const all = Array.from(doc.querySelectorAll('[role=option]')).filter(shown);
        const free = all.filter(o => {
          const box = o.closest('[role=listbox]');
          return !box || !box.hasAttribute('data-ar');
        });
        items = free.length ? free : all;
      }
    }
  }
  return items.filter(o => shown(o) || (o.tagName === 'INPUT' && o.labels && o.labels.length)).map((o, i) => {
    const ref = base + 'o' + i;
    o.setAttribute('data-ar', ref);
    const text = label(o);
    const value = o.tagName === 'INPUT' ? (o.value && o.value !== 'on' ? o.value : text)
                                         : (o.getAttribute('data-value') || o.getAttribute('value') || text);
    return {ref, text, value, selected: on(o), radio: o.tagName === 'INPUT',
            disabled: !!(o.disabled || o.getAttribute('aria-disabled') === 'true')};
  });
}"""

# Several positions on one page (a table row or a card each): the control at the same place in the row or card
# that shows `position`. Kept as it is when the page has no such row, or when the control already sits in it.
SCOPE_JS = r"""(el, pos) => {
  const ZW = /[​‌‍⁠﻿­]/g;
  const doc = el.ownerDocument;
  const esc = pos.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('(^|[^\\w.])' + esc + '(?![\\w]|\\.\\d)');
  const FIELDS = 'input:not([type=hidden]),select,textarea,[role=combobox],[role=listbox],[role=radiogroup]';
  const keep = {ref: null, inside: true};
  const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
  const found = [];
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    let a = t.parentElement;
    if (!a || !re.test(t.nodeValue.replace(ZW, ''))) continue;
    if (a.closest('select,option,script,style,[role=option],[hidden],[aria-hidden=true]')) continue;
    if (!a.getClientRects().length) continue;
    while (a && a !== doc.body && !a.querySelector(FIELDS)) a = a.parentElement;
    if (a && a !== doc.body) found.push(a);
  }
  if (!found.length) return keep;
  const count = c => c.querySelectorAll(FIELDS).length;
  found.sort((x, y) => count(x) - count(y));
  const box = found[0];
  if (box.contains(el)) return keep;
  // the row of the control and the row of the position: children of their closest common ancestor
  let mine = el;
  const path = [];
  while (mine.parentElement && !mine.parentElement.contains(box)) {
    path.unshift(Array.from(mine.parentElement.children).indexOf(mine));
    mine = mine.parentElement;
  }
  const common = mine.parentElement;
  if (!common) return {ref: null, inside: false};
  let row = box;
  while (row.parentElement !== common) row = row.parentElement;
  const shape = c => Array.from(c.querySelectorAll(FIELDS))
    .map(f => f.tagName + (f.type || '') + (f.getAttribute('role') || '')).join(',');
  if (row.tagName !== mine.tagName || shape(row) !== shape(mine)) return {ref: null, inside: false};
  let x = row;
  for (const i of path) x = x && x.children[i];
  if (!x || x.tagName !== el.tagName || (x.type || '') !== (el.type || '')) return {ref: null, inside: false};
  const ref = 'es' + Math.random().toString(36).slice(2, 10);
  x.setAttribute('data-ar', ref);
  return {ref, inside: true};
}"""


async def _select_state(element) -> dict:
    return await element.evaluate(
        "e => ({disabled: e.disabled, options: Array.from(e.options).map(o => ({value: o.value, "
        "text: (o.textContent || '').trim(), selected: o.selected, disabled: o.disabled}))})")


async def scoped(session, element, position: str):
    """`element`, or the control at the same place in the row or card of `position`; and whether it is in there."""
    if element is None or not position:
        return element, True
    try:
        found = await element.evaluate(SCOPE_JS, position)
    except Exception:
        return element, True
    if found.get("ref"):
        other = await locate(session.page, found["ref"])
        if other is not None:
            return other, True
    return element, bool(found.get("inside", True))


async def _ref(element) -> str:
    return (await element.get_attribute("data-ar")) or "ex"


async def choice_options(element, kind: str) -> list[dict]:
    """The options of a choice control, opening a closed combobox first."""
    if kind == "select":
        return (await _select_state(element))["options"]
    if kind == "combobox" and await element.get_attribute("aria-expanded") != "true":
        await element.click(timeout=5000)
        await asyncio.sleep(0.3)
    return await element.evaluate(OPTIONS_JS, await _ref(element))


async def pick_option(session, element, kind: str, option: dict) -> None:
    """Choose `option` (from choice_options) the way a person would: select it, tick its radio or click it."""
    if kind == "select":
        await element.select_option(value=option["value"], timeout=5000)
        return
    target = await locate(session.page, option["ref"])
    if target is None:
        raise LookupError(f"option {option['text']!r} vanished")
    if option.get("radio"):
        try:
            await target.check(timeout=3000)
        except Exception:  # a styled radio hides its input: its label takes the click
            await target.evaluate("e => (e.labels && e.labels[0] ? e.labels[0] : e).click()")
    else:
        await target.click(timeout=5000)
    await asyncio.sleep(0.15)


async def holds_option(element, kind: str, option: dict) -> bool:
    """Whether the choice control shows `option` as chosen."""
    if kind == "select":
        return await element.evaluate("e => e.value") == option["value"]
    if kind == "combobox":
        shown = clean(await element.evaluate(SHOWN_JS))
        if shown and clean(option["text"]) in shown:
            return True
    options = await element.evaluate(OPTIONS_JS, await _ref(element))
    return any(o["selected"] and clean(o["text"]) == clean(option["text"]) for o in options)


def choice_hint(view: str, plan: FillPlan, picked: dict[str, str], entry: dict) -> str:
    """
    The page's choices that no value is mapped to (a series or group that narrows the next list), with their
    options, for the model to pick through `choices`; '' when there are none.
    """
    mapped = {clean(label) for label in plan.fields} | set(picked)
    lines = []
    for element in ELEMENT.findall(view):
        found = CONTROL.match(element)
        if not found or found.group(2) not in CHOICE_KINDS or clean(found.group(3)) in mapped:
            continue
        options = element.split(" options: ", 1)[1].rstrip("]") if " options: " in element else ""
        lines.append(f"{found.group(3)!r}" + (f" (options: {options[:300]})" if options else ""))
    if not lines:
        return ""
    answer_keys = MONEY_KEYS | COUNT_KEYS | {"material_number", "position"}
    wanted = ", ".join(f"{k} {v}" for k, v in entry.items() if k not in answer_keys)
    return (f" No value is mapped to these choices of the page: {'; '.join(lines)}. If one of them must be set "
            f"first, call again with choices={{label: the option that fits this position"
            f"{f' ({wanted})' if wanted else ''}}}.")


async def _confirm(session, plan: FillPlan, view: str, position: str = "") -> tuple[str, str | None]:
    """Click the button the model named as the confirm of a choice; the new view, or the old one and an error."""
    button, why = find_control(view, plan.confirm, ("button",))
    target = await locate(session.page, button) if button else None
    target, _ = await scoped(session, target, position)
    if target is None:
        return view, why or f"{plan.confirm!r} vanished from the page"
    await target.click(timeout=5000)
    await session.settle()
    return await session.look(), None


async def fill_position(session, entry: dict, plan: FillPlan, log: list[str],
                        choices: dict[str, str] | None = None) -> str | None:
    """
    Enter one position's values into the form on screen, control by control in page order; returns an error
    message, or None when every mapped control holds its value.

    A choice (select, combobox, listbox, radio group) mapped to a material attribute (a key that is not an answer
    field) narrows the next one: after setting it, `plan.confirm` is clicked so the page offers the next level, and
    the choice must survive that round trip. A control still disabled (a choice from an earlier visit not yet
    confirmed) gets one confirm. `choices` (label -> option text) is the model's pick for such a choice, used where
    a worded attribute matches no option literally, and for choices no value is mapped to (a series the cascade
    needs); numbers must match exactly, and answer fields are never overridden. On a page with several positions,
    every control comes from the row or card of this position.
    """
    picked = {clean(label): text for label, text in (choices or {}).items()}
    answer_keys = MONEY_KEYS | COUNT_KEYS | {"material_number", "position"}
    # a position counts as entered only with its material, quantity, price and lead time (the material control
    # opens only once the choices before it are made)
    missing = sorted(set(REQUIRED_KEYS) - {"position"} - set(plan.fields.values()))
    if missing:
        return f"no control mapped for {', '.join(missing)}: add them to fields"
    position = str(entry.get("position") or "").strip()
    view = await session.look()
    order = {clean(label): i for i, (_, _, label) in enumerate(controls(view))}
    mapped = {clean(label) for label in plan.fields}
    steps: list[tuple[str, str | None]] = list(plan.fields.items())
    steps += [(label, None) for label in (choices or {}) if clean(label) not in mapped]
    steps.sort(key=lambda item: order.get(clean(item[0]), len(order)))
    for label, key in steps:
        if key is not None and key not in entry:
            log.append(f"{label}: left empty (this bid gives no value for it, which is fine)")
            continue
        value = form_value(key, entry[key], plan.decimal) if key else picked[clean(label)]
        element = None
        for attempt in range(2):
            ref, why = find_control(view, label, FIELD_KINDS)
            element = await locate(session.page, ref) if ref else None
            if element is None and ref is None and key not in REQUIRED_KEYS:
                # a label the model guessed for a value the form has no control for (live: "Druck" for the
                # pressure, which the form shows only inside the catalogue option): skip it, never fail the
                # position over it; the required values still need their controls
                break
            if element is None:
                return why or f"{label!r} vanished from the page"
            element, _ = await scoped(session, element, position)
            if not await element.evaluate(DISABLED_JS):
                break
            if attempt or not plan.confirm:
                return (f"{label!r} is disabled: an earlier choice was not confirmed or leads elsewhere "
                        f"(check the values chosen so far).{choice_hint(view, plan, picked, entry)}")
            # a choice made on an earlier visit may still wait for its confirm
            view, error = await _confirm(session, plan, view, position)
            if error:
                return error
        if element is None:
            log.append(f"{label}: no such control on this page; skipped")
            continue
        kind = await element.evaluate(KIND_JS)
        if kind in CHOICE_KINDS:
            options = await choice_options(element, kind)
            option, why = match_option(options, value)
            if (option is None and key is not None and key not in answer_keys and clean(label) in picked
                    and parse_number(value) is None):
                option, why = match_option(options, picked[clean(label)])  # the model's reading
            if option is None:
                if kind == "combobox":
                    await element.press("Escape")
                shown = " | ".join(o["text"] for o in options if o["value"])[:600]
                worded = key is not None and key not in answer_keys and parse_number(value) is None
                if worded:
                    hint = (f" If one option means the same as {value!r}, call again with choices={{{label!r}: "
                            f"<that option>}}.")
                else:  # a list narrowed by a choice nobody made yet, else a value the form does not offer
                    hint = choice_hint(view, plan, picked, entry) or " " + NUMBER_ONLY
                return f"{label!r}: {why}. Options: {shown}.{hint}"
            changed = not option.get("selected")
            if changed:
                await pick_option(session, element, kind, option)
                if not await holds_option(element, kind, option):
                    return f"{label!r} did not take {option['text']!r}"
            elif kind == "combobox":
                await element.press("Escape")
            log.append(f"{label} = {option['text']}")
            if key in answer_keys or not plan.confirm or not changed:
                continue
            view, error = await _confirm(session, plan, view, position)
            if error:
                return error
            ref, _ = find_control(view, label, CHOICE_KINDS)
            kept = await locate(session.page, ref) if ref else None
            kept, _ = await scoped(session, kept, position)
            if kept is None or not await holds_option(kept, kind, option):
                return f"{label!r} lost its choice after {plan.confirm!r}"
        else:
            numeric = key in MONEY_KEYS | COUNT_KEYS
            if kind == "number" and numeric:  # a number-only field takes a decimal point
                value = form_value(key, entry[key], ".")
            await element.fill(value, timeout=5000)
            if kind == "typeahead":  # a field that suggests options: take the one that means the value
                await asyncio.sleep(0.3)
                option, _ = match_option(await element.evaluate(OPTIONS_JS, await _ref(element)), value)
                if option is not None:
                    await pick_option(session, element, "combobox", option)
            shown = await element.evaluate(SHOWN_JS)
            if not (same_number(shown, value) if numeric else clean(value) in clean(shown)):
                return f"{label!r} did not take {value!r} (it shows {shown!r})"
            log.append(f"{label} = {value}")
    return None
