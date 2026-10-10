"""
Fill one bid position in a web form from calculated values: the mechanical part of the bid worker.

The model reads the form, says which position is on screen and which control takes which value (by the control's
label as its view shows it); `fill_position` then sets the controls in that order. Choices that narrow each other
(select one level, confirm, the next level's options appear) get the confirm button the model named after every
level. Options are matched by value, never by their place in the list, so text-sorted lists ("10, 125, 14, 8") and
look-alike neighbours ("63", "64") do not matter. Nothing here knows a site, a label or a value.
"""
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from extract import locate

ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
CONTROL = re.compile(r'\[(e\d+) (select|textbox|textarea|button|checkbox|radio) "((?:[^"\\]|\\.)*)"')
NUMBER_OPTION = re.compile(r"^\s*([+-]?\d[\d.,]*)\s*[^\d]*$")

# Values of a bid position: the answer fields from calc plus the material's attributes (keys of `attributes`).
MONEY_KEYS = {"unit_price", "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total"}
COUNT_KEYS = {"quantity", "lead_time_weeks"}
REQUIRED_KEYS = ("position", "material_number", "quantity", "unit_price", "lead_time_weeks")


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


def german(value, places: int = 2) -> str:
    """A number in German notation without thousands separators: 1234.5 -> "1234,50", places=0 -> "1235"."""
    number = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{number:.{places}f}".replace(".", ",")


def form_value(key: str, value) -> str:
    """The text a form field takes for a position value: money with two decimals, counts as integers."""
    if key in MONEY_KEYS:
        return german(value, 2)
    if key in COUNT_KEYS:
        return german(value, 0)
    return str(value)


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


def position_on_page(view: str, position: str) -> bool:
    """
    Whether the page is about this position: its number, as a whole token (01.04 is not 01.041 or 101.04), in a
    heading or in the text above the first choice or field (not among the options further down).
    """
    view = ZERO_WIDTH.sub("", view)
    first = re.search(r"\[e\d+ (select|textarea|checkbox|radio) ", view)
    head = view[:first.start()] if first else view
    head += "\n" + "\n".join(line for line in view.splitlines() if line.startswith("#"))
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
              if kind in ("select", "textbox", "textarea") and clean(label) not in mapped]
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
    """What the model said about the form: control label -> value key, and the button that confirms a choice."""

    fields: dict[str, str] = field(default_factory=dict)
    confirm: str = ""


async def _select_state(element) -> dict:
    return await element.evaluate(
        "e => ({disabled: e.disabled, options: Array.from(e.options).map(o => ({value: o.value, "
        "text: (o.textContent || '').trim(), selected: o.selected, disabled: o.disabled}))})")


async def _confirm(session, plan: FillPlan, view: str) -> tuple[str, str | None]:
    """Click the button the model named as the confirm of a choice; the new view, or the old one and an error."""
    button, why = find_control(view, plan.confirm, ("button",))
    target = await locate(session.page, button) if button else None
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

    A select mapped to a material attribute (a key that is not an answer field) is a choice that narrows the next
    one: after setting it, `plan.confirm` is clicked so the page offers the next level, and the choice must survive
    that round trip. A control still disabled (a choice from an earlier visit not yet confirmed) gets one confirm.
    `choices` (label -> option text) is the model's pick for such a choice, used only where a worded attribute
    matches no option literally; numbers must match exactly, and answer fields are never overridden.
    """
    picked = {clean(label): text for label, text in (choices or {}).items()}
    answer_keys = MONEY_KEYS | COUNT_KEYS | {"material_number", "position"}
    # a position counts as entered only with its material, quantity, price and lead time (the material control
    # opens only once the choices before it are made)
    unmapped = sorted(set(REQUIRED_KEYS) - {"position"} - set(plan.fields.values()))
    if unmapped:
        return f"no control mapped for {', '.join(unmapped)}: add them to fields"
    view = await session.look()
    order = {clean(label): i for i, (_, _, label) in enumerate(controls(view))}
    mapping = sorted(plan.fields.items(), key=lambda item: order.get(clean(item[0]), len(order)))
    for label, key in mapping:
        if key not in entry:
            log.append(f"{label}: left empty (this bid gives no value for it, which is fine)")
            continue
        value = form_value(key, entry[key])
        for attempt in range(2):
            ref, why = find_control(view, label, ("select", "textbox", "textarea"))
            element = await locate(session.page, ref) if ref else None
            if element is None:
                return why or f"{label!r} vanished from the page"
            if not await element.evaluate("e => e.disabled || e.readOnly === true"):
                break
            if attempt or not plan.confirm:
                return (f"{label!r} is disabled: an earlier choice was not confirmed or leads elsewhere "
                        f"(check the values chosen so far)")
            # a choice made on an earlier visit may still wait for its confirm
            view, error = await _confirm(session, plan, view)
            if error:
                return error
        if await element.evaluate("e => e.tagName") == "SELECT":
            state = await _select_state(element)
            option, why = match_option(state["options"], value)
            if option is None and key not in answer_keys and clean(label) in picked and parse_number(value) is None:
                option, why = match_option(state["options"], picked[clean(label)])  # the model's reading
            if option is None:
                shown = " | ".join(o["text"] for o in state["options"] if o["value"])[:600]
                worded = key not in answer_keys and parse_number(value) is None
                hint = (f" If one option means the same as {value!r}, call again with choices={{{label!r}: <that "
                        f"option>}}." if worded else " A number must match exactly: leave this position out.")
                return f"{label!r}: {why}. Options: {shown}.{hint}"
            changed = not option.get("selected")
            if changed:
                await element.select_option(value=option["value"], timeout=5000)
            log.append(f"{label} = {option['text']}")
            if key in answer_keys or not plan.confirm or not changed:
                continue
            view, error = await _confirm(session, plan, view)
            if error:
                return error
            ref, _ = find_control(view, label, ("select",))
            kept = await locate(session.page, ref) if ref else None
            if kept is None or await kept.evaluate("e => e.value") != option["value"]:
                return f"{label!r} lost its choice after {plan.confirm!r}"
        else:
            await element.fill(value, timeout=5000)
            if await element.evaluate("e => e.value") != value:
                return f"{label!r} did not take {value!r}"
            log.append(f"{label} = {value}")
    return None
