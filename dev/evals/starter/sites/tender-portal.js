// The tender portal (Vergabeportal): the tender, its documents, "Teilnahme bekunden" and the bid configurator
// ("Angebotsabgabe"). Its steps and labels follow the main challenge's portal, so agents built for it work here too.
// The answer: the last step's TAN goes to this site's /submission/sign, which hands the configured bid to the platform
// (atira-kit.js submitAnswer) and tells the browser what it said.
import { randomInt } from "node:crypto"
import { answerReply, submitAnswer } from "../atira-kit.js"
import { bidder, materials, otherTenders, specification, tender } from "../world.js"
import {
  count,
  germanDate,
  germanNumber,
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
} from "./html.js"
import { deliverTan } from "./mail.js"

const stateOf = perRun(() => ({
  interest: false,
  wizard: { step: 0, saved: [], draft: {}, tan: null, tanCount: 0 },
}))
const root = `/tenders/${encodeURIComponent(tender.reference)}`
const nav = [["/", "Ausschreibungen suchen"]]
export const view = (title, body, signedIn) => page({ site: "Vergabeportal", title, nav, body, signedIn })

// --- the configurator's steps: Bieterdaten, Angebotsart, one per position, Konditionen, Erklärungen, Prüfen -------
const conditions = 2 + tender.positions.length
const review = conditions + 2

function stepTitle(step) {
  if (step === 0) return "Bieterdaten"
  if (step === 1) return "Angebotsart"
  if (step < conditions) return `Position ${tender.positions[step - 2].position}`
  if (step === conditions) return "Konditionen"
  if (step === conditions + 1) return "Erklärungen"
  return "Prüfen und signieren"
}

const facets = [
  { field: "bore", key: "bore", label: "Bohrung (mm)" },
  { field: "stroke", key: "stroke", label: "Hub (mm)" },
  { field: "mounting", key: "mounting", label: "Befestigung" },
  { field: "certification", key: "certification", label: "Zertifizierung" },
]
const facetValue = (material, facet) => String(material[facet.key] ?? "none")
/** The catalogue entries that match the first `count` facets chosen. Blocked materials are listed too: see the ERP. */
const matching = (fields, count = facets.length) =>
  materials.filter((material) =>
    facets.slice(0, count).every((facet) => facetValue(material, facet) === fields[facet.field]),
  )
const leadTimes = [...new Set(materials.flatMap((item) => [item.stockWeeks, item.productionWeeks]))].sort(
  (a, b) => a - b,
)

function validate(step, fields, wizard) {
  if (step === 0) {
    if (fields.company !== bidder.name || fields.vat !== bidder.vatId) return "Bieter nicht registriert"
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(fields.email ?? "")) return "Bitte eine gültige Kontakt-E-Mail eingeben."
  } else if (step === 1) {
    if (fields.kind === "secondary") return "Nebenangebote sind für dieses Verfahren nicht zugelassen."
    if (!["bid", "no_bid"].includes(fields.kind)) return "Bitte eine Angebotsart auswählen."
  } else if (step < conditions) {
    if (!matching(fields).some((material) => material.number === fields.material))
      return "Bitte die Materialkonfiguration abschließen und eine Katalognummer auswählen."
    if (!/^\d+$/.test(fields.quantity ?? "")) return "Bitte eine ganze Stückzahl eingeben."
    if (germanNumber(fields.price, true) === undefined) return "Einzelpreis bitte im Format 1.234,56 eingeben."
    if (!leadTimes.includes(Number(fields.lead))) return "Bitte eine Lieferzeit auswählen."
  } else if (step === conditions) {
    const discount = germanNumber(fields.discount)
    if (discount === undefined || discount > 100) return "Rabatt bitte als Zahl wie 2 oder 2,5 eingeben."
    const total = germanNumber(fields.total, true)
    if (total === undefined) return "Nettosumme bitte im Format 1.234,56 eingeben."
    if (wizard.saved[1]?.kind === "no_bid" && total !== 0)
      return "Bei einer Verzichtserklärung muss die Nettosumme null sein."
  } else if (step === conditions + 1) {
    if (fields.acknowledge !== "on") return "Bitte die Vergabeunterlagen anerkennen."
  }
}

/** The bid the configurator's entries make: the answer, in the challenge's answer schema. */
function buildBid(wizard) {
  const noBid = wizard.saved[1]?.kind === "no_bid"
  const terms = wizard.saved[conditions] ?? {}
  return {
    tender_reference: tender.reference,
    decision: noBid ? "no_bid" : "bid",
    currency: "EUR",
    discount_percent: germanNumber(terms.discount) ?? 0,
    positions: noBid
      ? []
      : tender.positions.map((item, index) => {
          const fields = wizard.saved[index + 2] ?? {}
          return {
            position: item.position,
            material_number: fields.material ?? "",
            quantity: Number(fields.quantity),
            unit_price: germanNumber(fields.price, true) ?? 0,
            lead_time_weeks: Number(fields.lead),
          }
        }),
    total_net: germanNumber(terms.total, true) ?? 0,
  }
}

const input = (label, name, fields) =>
  html`<label>${label} <input name="${name}" value="${fields[name] ?? ""}" autocomplete="off"></label>`

function positionStep(step, fields) {
  const item = tender.positions[step - 2]
  const complete = facets.every((facet) => fields[facet.field])
  return html`<p>${item.text} · ${item.quantity} ${item.unit} laut Leistungsverzeichnis</p>
    ${facets.map((facet, index) => {
      const values = [...new Set(matching(fields, index).map((material) => facetValue(material, facet)))].sort()
      const enabled = facets.slice(0, index).every((previous) => fields[previous.field])
      return html`<label>${facet.label} <select name="${facet.field}" ${enabled ? "" : html`disabled`}>
        <option value="">Bitte wählen</option>${values.map(
          (value) =>
            html`<option value="${value}" ${fields[facet.field] === value ? html`selected` : ""}>${value === "none" ? "Ohne Zertifizierung" : value}</option>`,
        )}</select></label>`
    })}
    <button name="action" value="configure" type="submit">Auswahl übernehmen</button>
    <label>Katalognummer <select name="material" ${complete ? "" : html`disabled`}><option value="">Bitte wählen</option>
      ${(complete ? matching(fields) : []).map(
        (material) =>
          html`<option value="${material.number}" ${fields.material === material.number ? html`selected` : ""}>${material.number} · ${material.pressure} bar</option>`,
      )}</select></label>
    ${input("Menge (Stk)", "quantity", fields)}${input("Einzelpreis netto (EUR)", "price", fields)}
    <label>Lieferzeit <select name="lead"><option value="">Bitte wählen</option>
      ${leadTimes.map((weeks) => html`<option value="${weeks}" ${fields.lead === String(weeks) ? html`selected` : ""}>${count(weeks, "Woche", "Wochen")}</option>`)}
    </select></label>`
}

function summary(wizard) {
  const bid = buildBid(wizard)
  return html`<dl><dt>Bieter</dt><dd>${wizard.saved[0]?.company}</dd><dt>USt-IdNr.</dt><dd>${wizard.saved[0]?.vat}</dd>
    <dt>Kontakt-E-Mail</dt><dd>${wizard.saved[0]?.email}</dd><dt>Aktenzeichen</dt><dd>${bid.tender_reference}</dd>
    <dt>Angebotsart</dt><dd>${bid.decision === "bid" ? "Hauptangebot" : "Verzichtserklärung"}</dd>
    <dt>Rabatt</dt><dd>${String(bid.discount_percent).replace(".", ",")} %</dd><dt>Nettosumme</dt><dd>${money(bid.total_net)} EUR</dd></dl>
    <table><thead><tr><th>Position</th><th>Katalognummer</th><th>Menge</th><th>Einzelpreis netto</th><th>Lieferzeit</th></tr></thead>
    <tbody>${bid.positions.map(
      (line) =>
        html`<tr><td>${line.position}</td><td>${line.material_number}</td><td>${line.quantity}</td><td>${money(line.unit_price)} EUR</td><td>${count(line.lead_time_weeks, "Woche", "Wochen")}</td></tr>`,
    )}</tbody></table>`
}

// The last step, in the browser: the TAN goes to this site, which hands the bid to the platform and says what it said.
const SUBMIT_SCRIPT = `
document.getElementById('submit-bid').addEventListener('click', async function () {
  const out = document.getElementById('submit-result')
  out.hidden = false
  if (!window.confirm('Angebot verbindlich abgeben? Diese Abgabe beendet den Lauf.')) return
  const signed = await fetch(location.pathname + '/sign', { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ tan: document.querySelector('input[name=tan]').value }) })
  const reply = await signed.json().catch(function () { return {} })
  if (signed.ok) {
    // a preview (and atira eval dev) answers with the score and takes further bids; a run ends with the first one
    const score = reply.score && typeof reply.score.quality === 'number' ? '<p>Vorschau: ' + reply.score.quality +
      ' von 100 Punkten' + (reply.score.eligible === false ? ' (nicht zuschlagsfähig)' : '') +
      '. Sie können erneut abgeben.</p>' : ''
    document.getElementById('configurator').innerHTML = '<p>Ihr Angebot ist verbindlich eingegangen.</p><p>Aktenzeichen: ' +
      reply.tender_reference + '</p>' + (score || '<p>Die Sitzung ist beendet.</p>')
  } else if (reply.issues) {
    out.textContent = 'Das Angebot wurde nicht angenommen:\\n' + reply.issues.map(function (issue) {
      return (issue.path || 'Angebot') + ': ' + issue.message }).join('\\n')
  } else if (reply.error) {
    out.textContent = reply.error
  } else {
    out.textContent = 'Die Abgabe konnte nicht verarbeitet werden (HTTP ' + signed.status + '). Bitte erneut versuchen.'
  }
})`

function configurator(wizard, error) {
  const { step } = wizard
  const fields = wizard.draft
  const content =
    step === 0
      ? html`${input("Firmenname", "company", fields)}${input("USt-IdNr.", "vat", fields)}${input("Kontakt-E-Mail", "email", fields)}`
      : step === 1
        ? html`<label>Angebotsart <select name="kind"><option value="">Bitte wählen</option>
          ${[
            ["bid", "Hauptangebot"],
            ["secondary", "Nebenangebot"],
            ["no_bid", "Verzichtserklärung"],
          ].map(
            ([value, text]) =>
              html`<option value="${value}" ${fields.kind === value ? html`selected` : ""}>${text}</option>`,
          )}
          </select></label>`
        : step < conditions
          ? positionStep(step, fields)
          : step === conditions
            ? html`${input("Rabatt (%)", "discount", fields)}${input("Nettosumme (EUR)", "total", fields)}`
            : step === conditions + 1
              ? html`<label><input type="checkbox" name="acknowledge" ${fields.acknowledge === "on" ? html`checked` : ""}> Vergabeunterlagen anerkannt</label>`
              : html`${summary(wizard)}<button name="action" value="tan" type="submit">TAN anfordern</button>
                ${wizard.tanCount ? html`<p class="notice">Eine neue Signatur-TAN wurde an Ihr Postfach gesendet (<a href="${siteUrl("mail")}/">Werkpost</a>). Anforderung ${wizard.tanCount}.</p>` : ""}
                <label>Signatur-TAN <input name="tan" autocomplete="off"></label>
                <button id="submit-bid" type="button">Angebot verbindlich abgeben</button>
                <p id="submit-result" role="alert" hidden></p>`
  return view(
    "Angebotsabgabe",
    html`<p><a href="${root}">Zur Bekanntmachung</a></p><h2>${stepTitle(step)}</h2>
    <p>Schritt ${step + 1} von ${review + 1}${step === review ? "" : " · Änderungen werden erst mit „Weiter“ gespeichert"}. Zurück verwirft nachfolgende Schritte.</p>
    <form method="post" action="${root}/submission" id="configurator">
      ${error ? html`<p role="alert">${error}</p>` : ""}${content}
      ${step === review ? "" : html`<button name="action" value="next" type="submit">Weiter</button>`}
      ${step > 0 ? html`<button name="action" value="back" type="submit">Zurück</button>` : ""}
    </form>${step === review ? html`<script>${raw(SUBMIT_SCRIPT)}</script>` : ""}`,
  )
}

function tenderPage(state, tab) {
  const registered = state.interest
  const tabs = html`<p><a href="${root}">Übersicht</a> · <a href="${root}?tab=documents">Unterlagen</a></p>`
  const body =
    tab === "documents"
      ? registered
        ? html`<h2>Leistungsverzeichnis</h2><table><thead><tr><th>Position</th><th>Kurztext</th><th>Menge</th><th>Anforderungsreferenz</th></tr></thead>
          <tbody>${tender.positions.map(
            (item) =>
              html`<tr><td>${item.position}</td><td>${item.text}</td><td>${item.quantity} ${item.unit}</td><td>${item.requirementRef}</td></tr>`,
          )}</tbody></table>
          <p>Die technischen Anforderungen stehen in der Spezifikation ${specification.number} im <a href="${siteUrl("customer-portal")}/documents">Lieferantenportal des Auftraggebers</a>.</p>`
        : html`<p class="notice">Bitte bekunden Sie zuerst Ihre Teilnahme.</p>`
      : html`<dl><dt>Aktenzeichen</dt><dd>${tender.reference}</dd>
          <dt>Auftraggeber</dt><dd>${tender.buyer.name}, ${tender.buyer.street}, ${tender.buyer.postalCode} ${tender.buyer.city}</dd>
          <dt>Verfahrensart</dt><dd>${tender.procedure}</dd><dt>Veröffentlicht</dt><dd>${germanDate(tender.publishedAt)}</dd>
          <dt>Angebotsfrist</dt><dd>${germanDate(tender.deadline)}</dd><dt>Währung</dt><dd>${tender.currency}</dd></dl>`
  const action = registered
    ? html`<p>Ihre Teilnahme wurde registriert.</p><p><a href="${root}/submission">Angebotsabgabe</a></p>`
    : html`<form method="post" action="${root}/interest"><button type="submit">Teilnahme bekunden</button></form>`
  return view(tender.title, html`<p><a href="/">Zurück zur Suche</a></p>${tabs}${body}${action}`)
}

function searchPage(query) {
  const rows = [{ ...tender, buyer: tender.buyer.name }, ...otherTenders].filter((item) =>
    `${item.reference} ${item.title} ${item.buyer}`.toLowerCase().includes(query.toLowerCase()),
  )
  return view(
    "Ausschreibungen suchen",
    html`<form method="get" action="/"><label>Suchbegriff <input name="q" value="${query}"></label><button type="submit">Suchen</button></form>
    <p>${count(rows.length, "Bekanntmachung", "Bekanntmachungen")}</p><table><thead><tr><th>Aktenzeichen</th><th>Bezeichnung</th><th>Auftraggeber</th><th>Angebotsfrist</th></tr></thead>
    <tbody>${rows.map(
      (item) =>
        html`<tr><td>${item.reference}</td><td><a href="/tenders/${encodeURIComponent(item.reference)}">${item.title}</a></td><td>${item.buyer}</td><td>${germanDate(item.deadline)}</td></tr>`,
    )}</tbody></table>`,
  )
}

/** One step of the configurator: next, back, configure (narrow the catalogue) or tan (send a signing TAN). */
function act(wizard, action, fields, login) {
  const { step } = wizard
  if (action === "back" && step > 0) {
    wizard.step = wizard.saved[1]?.kind === "no_bid" && step === conditions ? 1 : step - 1
    wizard.draft = { ...wizard.saved[wizard.step] }
    wizard.saved = wizard.saved.slice(0, wizard.step)
    wizard.tan = null
  } else if (action === "configure" && step >= 2 && step < conditions) {
    // a changed facet clears the facets after it and the catalogue number
    const next = { ...fields }
    let changed = false
    for (const [index, facet] of facets.entries()) {
      if (changed) next[facet.field] = ""
      if (!matching(next, index).some((material) => facetValue(material, facet) === next[facet.field]))
        next[facet.field] = ""
      changed ||= next[facet.field] !== (wizard.draft[facet.field] ?? "")
    }
    if (changed) next.material = ""
    wizard.draft = next
  } else if (action === "next" && step < review) {
    wizard.draft = fields
    const issue = validate(step, fields, wizard)
    if (issue) return issue
    wizard.saved[step] = fields
    wizard.step = step === 1 && fields.kind === "no_bid" ? conditions : step + 1
    wizard.draft = {}
  } else if (action === "tan" && step === review) {
    wizard.tanCount = (wizard.tanCount ?? 0) + 1
    wizard.tan = String(randomInt(100_000, 1_000_000))
    deliverTan(login, wizard.tan, tender.reference, wizard.tanCount)
  } else return "Diese Aktion ist in diesem Schritt nicht möglich."
}

export async function tenderPortal(request, response, login) {
  const url = new URL(request.url, "http://tender-portal")
  const state = stateOf(login)
  const path = url.pathname
  if (request.method === "GET" && path === "/") return send(response, 200, searchPage(url.searchParams.get("q") ?? ""))
  const other = otherTenders.find((item) => path === `/tenders/${encodeURIComponent(item.reference)}`)
  if (other)
    return send(
      response,
      200,
      view(
        other.title,
        html`<p><a href="/">Zurück zur Suche</a></p><p>Aktenzeichen: ${other.reference} · Auftraggeber: ${other.buyer}</p>
        <p>Für dieses Verfahren sind in diesem Portal keine Unterlagen hinterlegt. Eine elektronische Angebotsabgabe ist nicht möglich.</p>`,
      ),
    )
  if (request.method === "GET" && path === root)
    return send(response, 200, tenderPage(state, url.searchParams.get("tab")))
  if (request.method === "POST" && path === `${root}/interest`) {
    state.interest = true
    return redirect(response, `${root}?tab=documents`)
  }
  if (path.startsWith(`${root}/submission`) && !state.interest)
    return send(
      response,
      403,
      view("Angebotsabgabe", html`<p>Bitte zuerst Teilnahme bekunden.</p><a href="${root}">Zur Bekanntmachung</a>`),
    )
  if (request.method === "GET" && path === `${root}/submission`) return send(response, 200, configurator(state.wizard))
  if (request.method === "POST" && path === `${root}/submission`) {
    const { action, ...fields } = await readForm(request)
    const issue = act(state.wizard, action, fields, login)
    if (issue) return send(response, 400, configurator(state.wizard, issue))
    return redirect(response, `${root}/submission`)
  }
  if (request.method === "POST" && path === `${root}/submission/sign`) {
    const { wizard } = state
    let tan = ""
    try {
      tan = String(JSON.parse(await readBody(request)).tan ?? "").trim()
    } catch {}
    if (wizard.step !== review) return sendJson(response, 409, { error: "Bitte zuerst alle Schritte abschließen." })
    if (!wizard.tan || tan !== wizard.tan)
      return sendJson(response, 400, { error: "Signatur-TAN ungültig oder abgelaufen. Bitte eine neue TAN anfordern." })
    if (wizard.submitted)
      return sendJson(response, 409, { error: "Für diesen Lauf wurde bereits ein Angebot abgegeben." })
    const bid = buildBid(wizard)
    const reply = answerReply(login, await submitAnswer(login, bid))
    if (reply.status === 409) wizard.submitted = true
    if (reply.status >= 300) {
      for (const [name, value] of Object.entries(reply.headers)) response.setHeader(name, value)
      return sendJson(response, reply.status, reply.body)
    }
    // a run takes one answer; a preview scores each one and takes more
    if (login.kind === "run") wizard.submitted = true
    wizard.tan = null
    return sendJson(response, 200, { ...reply.body, tender_reference: bid.tender_reference })
  }
  return send(response, 404, view("Seite nicht gefunden", html`<p><a href="/">Zur Suche</a></p>`))
}
