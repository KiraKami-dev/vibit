// The sourcing hub: open tenders, each notice with sections in a sidebar (Notice, Items, Documents, Bid), "Register to
// bid", and the bid form. The bid form differs from the public scenario's wizard on purpose: line items sit in a table,
// three per page with repeated column labels, each with a series (catalogue group) select, a material combobox
// (ARIA listbox, not a native select), a lead-time radio group and English numbers ("1,234.56"). The signing code
// comes by mail; the last step hands the bid to the platform (atira-kit.js submitAnswer).
import { randomInt } from "node:crypto"
import { answerReply, submitAnswer } from "../atira-kit.js"
import { bidder, groups, materials, otherTenders, tender } from "../world.js"
import {
  day,
  englishNumber,
  html,
  money,
  page,
  perRun,
  raw,
  readBody,
  readForm,
  redirect,
  send,
  sendJson,
  siteUrl,
  stamp,
  weeks,
} from "./html.js"
import { deliverTan } from "./inbox.js"

const stateOf = perRun(() => ({ registered: false, form: { step: 0, saved: [], draft: {}, tan: null, codes: 0 } }))
const root = `/notices/${encodeURIComponent(tender.reference)}`
const sideNav = (base) => [
  ["/", "Open tenders"],
  [`${base}`, "Notice"],
  [`${base}/items`, "Lots & items"],
  [`${base}/documents`, "Documents"],
  [`${base}/bid`, "Bid"],
]
export const view = (title, body, signedIn, nav = [["/", "Open tenders"]]) =>
  page({ site: "Sourcing Hub", title, nav, body, accent: "#1f4e79", signedIn, side: true })

// --- the bid form's steps -----------------------------------------------------------------------------------------
const PER_PAGE = 3
const linePages = Math.ceil(tender.positions.length / PER_PAGE)
const TERMS = 2 + linePages
const DECLARATIONS = TERMS + 1
const REVIEW = TERMS + 2
const LEAD_OPTIONS = Array.from({ length: 12 }, (_, i) => i + 1)

const stepTitle = (step) =>
  step === 0
    ? "Company details"
    : step === 1
      ? "Type of bid"
      : step < TERMS
        ? `Line items (page ${step - 1} of ${linePages})`
        : step === TERMS
          ? "Commercial terms"
          : step === DECLARATIONS
            ? "Declarations"
            : "Review and sign"
const linesOf = (step) => tender.positions.slice((step - 2) * PER_PAGE, (step - 1) * PER_PAGE)
const catalogue = materials.map((m) => ({
  number: m.number,
  group: m.group,
  text: `${m.number}: ${m.bore}/${m.stroke}, ${m.pressure} bar, ${m.mounting}${m.certification ? `, ${m.certification}` : ""}`,
}))
const BREAKDOWN = [
  ["base", "Base price per piece (EUR)", "base_unit_price"],
  ["surcharge", "Surcharge per piece (EUR)", "surcharge_per_unit"],
  ["packaging", "Packaging per piece (EUR)", "packaging_per_unit"],
  ["rebate", "Project bonus, whole line (EUR)", "rebate_total"],
]

function validateLine(item, f) {
  const p = item.position
  if (f[`omit_${p}`] === "on") return []
  const issues = []
  const series = f[`series_${p}`]
  if (!groups[series]) issues.push(`Line ${p}: choose a series.`)
  const material = materials.find((m) => m.number === (f[`material_${p}`] ?? "").trim())
  if (!material) issues.push(`Line ${p}: choose a material from the list.`)
  else if (material.group !== series) issues.push(`Line ${p}: the material does not belong to the selected series.`)
  if (!/^\d+$/.test((f[`qty_${p}`] ?? "").trim())) issues.push(`Line ${p}: enter the quantity as a whole number of pieces.`)
  if (englishNumber(f[`price_${p}`], true) === undefined)
    issues.push(`Line ${p}: enter the unit price in the format 1,234.56.`)
  if (!LEAD_OPTIONS.includes(Number(f[`lead_${p}`]))) issues.push(`Line ${p}: choose a lead time.`)
  for (const [key, text] of BREAKDOWN) {
    const value = (f[`${key}_${p}`] ?? "").trim()
    if (value && englishNumber(value, true) === undefined) issues.push(`Line ${p}: ${text} must look like 1,234.56 or stay empty.`)
  }
  return issues
}

function validate(step, f, form) {
  if (step === 0) {
    if ((f.company ?? "").trim() !== bidder.name || (f.vat ?? "").trim() !== bidder.vatId)
      return "This bidder is not registered on the hub. Check the company name and VAT ID."
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(f.email ?? "")) return "Enter a valid contact e-mail."
  } else if (step === 1) {
    if (f.kind === "alternative") return "Alternative bids are not admitted in this procedure."
    if (!["bid", "no_bid"].includes(f.kind)) return "Choose the type of bid."
  } else if (step < TERMS) {
    const issues = linesOf(step).flatMap((item) => validateLine(item, f))
    if (issues.length) return issues.join("\n")
  } else if (step === TERMS) {
    const discount = englishNumber(f.discount)
    if (discount === undefined || discount > 100) return "Enter the discount as a number such as 3 or 4.5."
    const total = englishNumber(f.total, true)
    if (total === undefined) return "Enter the net total in the format 1,234.56."
    if (form.saved[1]?.kind === "no_bid" && total !== 0) return "A declined bid has a net total of 0.00."
  } else if (step === DECLARATIONS) {
    if (f.own_terms === "on") return "Bids subject to the bidder's own terms and conditions are excluded."
    if (f.accept === "on") return
    return "Accept the tender documents to continue."
  }
}

const lineFields = (form) => Object.assign({}, ...form.saved.slice(2, TERMS).filter(Boolean))

/** The bid the form's entries make: the answer, in the challenge's answer schema. */
function buildBid(form) {
  const noBid = form.saved[1]?.kind === "no_bid"
  const terms = form.saved[TERMS] ?? {}
  const f = lineFields(form)
  const positions = noBid
    ? []
    : tender.positions
        .filter((item) => f[`omit_${item.position}`] !== "on")
        .map((item) => {
          const p = item.position
          const line = {
            position: p,
            material_number: (f[`material_${p}`] ?? "").trim(),
            quantity: Number(f[`qty_${p}`]),
            unit_price: englishNumber(f[`price_${p}`], true) ?? 0,
            lead_time_weeks: Number(f[`lead_${p}`]),
          }
          for (const [key, , field] of BREAKDOWN) {
            const value = englishNumber(f[`${key}_${p}`], true)
            if (value !== undefined) line[field] = value
          }
          return line
        })
  return {
    tender_reference: tender.reference,
    decision: noBid ? "no_bid" : "bid",
    currency: "EUR",
    discount_percent: englishNumber(terms.discount) ?? 0,
    positions,
    total_net: englishNumber(terms.total, true) ?? 0,
  }
}

const text = (labelText, name, f, attrs = "") =>
  html`<label class="field">${labelText} <input name="${name}" value="${f[name] ?? ""}" autocomplete="off" ${raw(attrs)}></label>`

function linePage(step, f) {
  const rows = linesOf(step).map((item) => {
    const p = item.position
    const series = f[`series_${p}`] ?? ""
    return html`<tbody data-line="${p}"><tr><td rowspan="2"><strong>${p}</strong></td>
      <td>${item.text}<br><small>${item.quantity} ${item.unit} in the bill of items</small></td>
      <td><label>Series <select name="series_${p}" data-series="${p}"><option value="">Select…</option>${Object.entries(groups).map(
        ([key, name]) => html`<option value="${key}" ${series === key ? html`selected` : ""}>${key} (${name})</option>`,
      )}</select></label></td>
      <td><div class="combo"><label>Material <input role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="list_${p}"
        name="material_${p}" value="${f[`material_${p}`] ?? ""}" autocomplete="off" placeholder="Type or pick" data-line="${p}"></label>
        <ul role="listbox" id="list_${p}" hidden></ul></div></td>
      <td><label>Qty <input name="qty_${p}" value="${f[`qty_${p}`] ?? ""}" size="6" autocomplete="off"></label></td>
      <td><label>Unit price (EUR) <input name="price_${p}" value="${f[`price_${p}`] ?? ""}" size="10" placeholder="1,234.56" autocomplete="off"></label></td>
      <td><label><input type="checkbox" name="omit_${p}" ${f[`omit_${p}`] === "on" ? html`checked` : ""}> Omit this line</label></td></tr>
      <tr><td colspan="5"><fieldset role="radiogroup" aria-label="Lead time line ${p}"><legend>Lead time</legend>${LEAD_OPTIONS.map(
        (w) =>
          html`<label><input type="radio" name="lead_${p}" value="${w}" ${f[`lead_${p}`] === String(w) ? html`checked` : ""}> ${weeks(w)}</label>`,
      )}</fieldset>
      <details><summary>Price breakdown (optional)</summary>${BREAKDOWN.map(
        ([key, labelText]) =>
          html`<label>${labelText} <input name="${key}_${p}" value="${f[`${key}_${p}`] ?? ""}" size="10" placeholder="0.00" autocomplete="off"></label> `,
      )}</details></td></tr></tbody>`
  })
  return html`<table><thead><tr><th>Line</th><th>Item</th><th>Series</th><th>Material</th><th>Qty</th><th>Unit price</th><th></th></tr></thead>${rows}</table>
    <p><small>Quantities in pieces. Prices net in EUR, English format (1,234.56). Unit prices include all surcharges, setup and packaging per piece.</small></p>
    <script>${raw(`window.CATALOGUE = ${JSON.stringify(catalogue)};`)}${raw(COMBO_SCRIPT)}</script>`
}

// The material combobox: options of the chosen series, filtered by what is typed; a click puts the number in the field.
const COMBO_SCRIPT = `
document.querySelectorAll('input[role=combobox]').forEach(function (input) {
  var line = input.dataset.line
  var list = document.getElementById('list_' + line)
  var series = document.querySelector('select[data-series="' + line + '"]')
  function render() {
    var typed = input.value.trim().toLowerCase()
    var options = window.CATALOGUE.filter(function (m) {
      return m.group === series.value && (!typed || m.text.toLowerCase().indexOf(typed) >= 0 || m.number.toLowerCase() === typed)
    })
    list.innerHTML = ''
    options.forEach(function (m, i) {
      var li = document.createElement('li')
      li.setAttribute('role', 'option')
      li.id = 'opt_' + line + '_' + i
      li.textContent = m.text
      li.addEventListener('mousedown', function (event) {
        event.preventDefault()
        input.value = m.number
        close()
      })
      list.appendChild(li)
    })
    if (!series.value) { var hint = document.createElement('li'); hint.textContent = 'Select a series first'; hint.setAttribute('aria-disabled', 'true'); list.appendChild(hint) }
    list.hidden = false
    input.setAttribute('aria-expanded', 'true')
  }
  function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false') }
  input.addEventListener('focus', render)
  input.addEventListener('input', render)
  input.addEventListener('blur', function () { setTimeout(close, 150) })
  series.addEventListener('change', function () { input.value = ''; close() })
})`

function terms(f, form) {
  const net = buildBid({ ...form, saved: [...form.saved.slice(0, TERMS), { discount: "0", total: "0.00" }] }).positions.reduce(
    (sum, line) => sum + line.quantity * line.unit_price,
    0,
  )
  return html`${text("Discount (%)", "discount", f, 'placeholder="4.5"')}${text("Net total (EUR)", "total", f, 'placeholder="12,345.67"')}
    <p>Gross amount incl. 19 % VAT (for information): <span data-field="total_net">${money(Math.round(net * 119) / 100)}</span> EUR</p>`
}

function summary(form) {
  const bid = buildBid(form)
  return html`<dl><dt>Bidder</dt><dd>${form.saved[0]?.company}</dd><dt>VAT ID</dt><dd>${form.saved[0]?.vat}</dd>
    <dt>Contact</dt><dd>${form.saved[0]?.email}</dd><dt>Tender</dt><dd>${bid.tender_reference}</dd>
    <dt>Type of bid</dt><dd>${bid.decision === "bid" ? "Main bid" : "Decline to bid"}</dd>
    <dt>Discount</dt><dd>${bid.discount_percent} %</dd><dt>Net total</dt><dd>${money(bid.total_net)} EUR</dd></dl>
    <table><thead><tr><th>Line</th><th>Material</th><th>Qty</th><th>Unit price</th><th>Lead time</th></tr></thead>
    <tbody>${bid.positions.map(
      (line) =>
        html`<tr><td>${line.position}</td><td>${line.material_number}</td><td>${line.quantity}</td><td>${money(line.unit_price)} EUR</td><td>${weeks(line.lead_time_weeks)}</td></tr>`,
    )}</tbody></table>`
}

const SUBMIT_SCRIPT = `
document.getElementById('submit-bid').addEventListener('click', async function () {
  const out = document.getElementById('submit-result')
  out.hidden = false
  if (!window.confirm('Submit this bid as binding? This ends your session.')) return
  const signed = await fetch(location.pathname + '/sign', { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code: document.querySelector('input[name=code]').value }) })
  const reply = await signed.json().catch(function () { return {} })
  if (signed.ok) {
    const score = reply.score && typeof reply.score.quality === 'number' ? '<p>Preview: ' + reply.score.quality +
      ' of 100 points. You can submit again.</p>' : ''
    document.getElementById('bid-form').innerHTML = '<p>Your bid has been received and is binding.</p><p>Tender: ' +
      reply.tender_reference + '</p>' + (score || '<p>The session has ended.</p>')
  } else if (reply.issues) {
    out.textContent = 'The bid was not accepted:\\n' + reply.issues.map(function (issue) {
      return (issue.path || 'bid') + ': ' + issue.message }).join('\\n')
  } else {
    out.textContent = reply.error || ('The submission failed (HTTP ' + signed.status + '). Please try again.')
  }
})`

function bidForm(form, error) {
  const { step } = form
  const f = form.draft
  let content
  if (step === 0)
    content = html`${text("Company name", "company", f)}${text("VAT ID", "vat", f)}${text("Contact e-mail", "email", f)}`
  else if (step === 1)
    content = html`<fieldset role="radiogroup" aria-label="Type of bid">${[
      ["alternative", "Alternative bid"],
      ["bid", "Main bid"],
      ["no_bid", "Decline to bid"],
    ].map(
      ([value, labelText]) =>
        html`<label style="display:block;margin:6px 0"><input type="radio" name="kind" value="${value}" ${f.kind === value ? html`checked` : ""}> ${labelText}</label>`,
    )}</fieldset>`
  else if (step < TERMS) content = linePage(step, f)
  else if (step === TERMS) content = terms(f, form)
  else if (step === DECLARATIONS)
    content = html`<label class="field"><input type="checkbox" name="own_terms" ${f.own_terms === "on" ? html`checked` : ""}> Our own general terms and conditions apply</label>
      <label class="field"><input type="checkbox" name="accept" ${f.accept === "on" ? html`checked` : ""}> I accept the tender documents</label>`
  else
    content = html`${summary(form)}<button name="action" value="code" type="submit">Request signing code</button>
      ${form.codes ? html`<p class="notice">Signing code ${form.codes} was sent to your mailbox (<a href="${siteUrl("inbox")}/">Mail</a>).</p>` : ""}
      <label class="field">Signing code <input name="code" autocomplete="off"></label>
      <button id="submit-bid" type="button">Submit binding bid</button>
      <p id="submit-result" role="alert" hidden></p>`
  return view(
    `Bid for ${tender.reference}`,
    html`<p>Step ${step + 1} of ${REVIEW + 1}: <strong>${stepTitle(step)}</strong>${step === REVIEW ? "" : " · entries are saved with “Save and continue”"}</p>
    <form method="post" action="${root}/bid" id="bid-form">
      ${error ? html`<p role="alert">${error}</p>` : ""}${content}
      ${step > 0 ? html`<button name="action" value="back" type="submit">Back</button>` : ""}
      ${step === REVIEW ? "" : html`<button name="action" value="next" type="submit">Save and continue</button>`}
    </form>${step === REVIEW ? html`<script>${raw(SUBMIT_SCRIPT)}</script>` : ""}`,
    true,
    sideNav(root),
  )
}

function act(form, action, fields, login) {
  const { step } = form
  if (action === "back" && step > 0) {
    form.step = form.saved[1]?.kind === "no_bid" && step === TERMS ? 1 : step - 1
    form.draft = { ...form.saved[form.step] }
    form.tan = null
  } else if (action === "next" && step < REVIEW) {
    form.draft = fields
    const issue = validate(step, fields, form)
    if (issue) return issue
    form.saved[step] = fields
    form.step = step === 1 && fields.kind === "no_bid" ? TERMS : step + 1
    form.draft = { ...(form.saved[form.step] ?? {}) }
  } else if (action === "code" && step === REVIEW) {
    form.codes += 1
    form.tan = String(randomInt(100_000, 1_000_000))
    form.tanAt = Date.now()
    deliverTan(login, form.tan, tender.reference, form.codes)
  } else return "This action is not available in this step."
}

// --- notice pages -------------------------------------------------------------------------------------------------
function noticePage(item, section, registered, own) {
  const base = `/notices/${encodeURIComponent(item.reference)}`
  const positions = item.positions
  let body
  if (section === "items")
    body = registered
      ? html`<table><thead><tr><th>Line</th><th>Item</th><th>Quantity</th><th>Unit</th><th>Requirement</th></tr></thead><tbody>${positions.map(
          (p) => html`<tr><td>${p.position}</td><td>${p.text}</td><td>${p.quantity}</td><td>${p.unit}</td><td>${p.requirementRef ?? "-"}</td></tr>`,
        )}</tbody></table>`
      : html`<p class="notice">Register to bid to see the items.</p>`
  else if (section === "documents")
    body = registered
      ? own
        ? html`<ul><li>Tender conditions: bids are submitted only on this hub, net in EUR.</li>
            <li>Technical specification TS-4471 and all bidder clarifications: on the buyer's <a href="${siteUrl("buyer-extranet")}/">Supplier Extranet</a>.</li></ul>`
        : html`<p>No documents are published for this notice.</p>`
      : html`<p class="notice">Register to bid to see the documents.</p>`
  else if (section === "bid")
    body = html`<p class="notice">Electronic submission is not open for this notice.</p>`
  else
    body = html`<dl><dt>Reference</dt><dd>${item.reference}</dd><dt>Contracting authority</dt><dd>${typeof item.buyer === "string" ? item.buyer : item.buyer.name}</dd>
      ${own ? html`<dt>Procedure</dt><dd>${tender.procedure}</dd><dt>Published</dt><dd>${day(tender.publishedAt)}</dd>` : ""}
      <dt>Submission deadline</dt><dd>${stamp(item.deadline)}</dd></dl>
      ${registered ? html`<p>You are registered for this notice.</p>` : html`<form method="post" action="${base}/register"><button type="submit">Register to bid</button></form>`}`
  return view(item.title, body, true, sideNav(base))
}

function listPage(query) {
  const all = [{ ...tender, buyer: tender.buyer.name }, ...otherTenders]
  const rows = all.filter((item) => `${item.reference} ${item.title} ${item.buyer}`.toLowerCase().includes(query.toLowerCase()))
  return view(
    "Open tenders",
    html`<form method="get" action="/"><label>Search <input name="q" value="${query}"></label> <button type="submit">Search</button></form>
    <table><thead><tr><th>Reference</th><th>Title</th><th>Contracting authority</th><th>Deadline</th></tr></thead><tbody>${rows.map(
      (item) =>
        html`<tr><td>${item.reference}</td><td><a href="/notices/${encodeURIComponent(item.reference)}">${item.title}</a></td><td>${item.buyer}</td><td>${stamp(item.deadline)}</td></tr>`,
    )}</tbody></table>`,
  )
}

export async function sourcingHub(request, response, login) {
  const url = new URL(request.url, "http://sourcing-hub")
  const state = stateOf(login)
  const path = url.pathname
  if (request.method === "GET" && path === "/") return send(response, 200, listPage(url.searchParams.get("q") ?? ""))
  for (const other of otherTenders) {
    const base = `/notices/${encodeURIComponent(other.reference)}`
    if (request.method === "POST" && path === `${base}/register`) return redirect(response, `${base}/items`)
    const section = path === base ? "notice" : path.startsWith(`${base}/`) ? path.slice(base.length + 1) : null
    if (request.method === "GET" && section) return send(response, 200, noticePage(other, section, true, false))
  }
  if (request.method === "POST" && path === `${root}/register`) {
    state.registered = true
    return redirect(response, `${root}/items`)
  }
  if (path === `${root}/bid` || path === `${root}/bid/sign`) {
    if (!state.registered)
      return send(response, 403, view("Bid", html`<p class="notice">Register to bid first.</p><p><a href="${root}">Notice</a></p>`, true, sideNav(root)))
    const { form } = state
    if (request.method === "GET" && path === `${root}/bid`) return send(response, 200, bidForm(form))
    if (request.method === "POST" && path === `${root}/bid`) {
      const { action, ...fields } = await readForm(request)
      const issue = act(form, action, fields, login)
      if (issue) return send(response, 400, bidForm(form, issue))
      return redirect(response, `${root}/bid`)
    }
    if (request.method === "POST" && path === `${root}/bid/sign`) {
      let code = ""
      try {
        code = String(JSON.parse(await readBody(request)).code ?? "").trim()
      } catch {}
      if (form.step !== REVIEW) return sendJson(response, 409, { error: "Complete all steps first." })
      if (!form.tan || code !== form.tan || Date.now() - form.tanAt > 300_000)
        return sendJson(response, 400, { error: "Signing code invalid or expired. Request a new code." })
      if (form.submitted) return sendJson(response, 409, { error: "A bid has already been submitted in this session." })
      const bid = buildBid(form)
      const reply = answerReply(login, await submitAnswer(login, bid))
      if (reply.status === 409) form.submitted = true
      if (reply.status >= 300) {
        for (const [name, value] of Object.entries(reply.headers)) response.setHeader(name, value)
        return sendJson(response, reply.status, reply.body)
      }
      if (login.kind === "run") form.submitted = true
      form.tan = null
      return sendJson(response, 200, { ...reply.body, tender_reference: bid.tender_reference })
    }
  }
  if (request.method === "GET" && (path === root || path.startsWith(`${root}/`))) {
    const section = path === root ? "notice" : path.slice(root.length + 1)
    if (["notice", "items", "documents"].includes(section))
      return send(response, 200, noticePage(tender, section, state.registered, true))
  }
  return send(response, 404, view("Page not found", html`<p><a href="/">Open tenders</a></p>`))
}
