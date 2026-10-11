// The bidder's ERP: a material search (catalogue group, number or name; at most 20 hits) and the material master
// with attributes, status, free and blocked stock, lead times, surcharge mass, metal index and price conditions.
import { bidder, materials } from "../world.js"
import { count, germanDate, html, money, page, send, siteUrl } from "./html.js"

const MAX_HITS = 20
const nav = () => [
  ["/", "Materialsuche"],
  [`${siteUrl("mail")}/`, "Werkpost"],
]
export const view = (title, body, signedIn) =>
  page({ site: `ERP · ${bidder.name}`, title, nav: nav(), body, accent: "#5b4a2f", signedIn })

const kg = (value) => `${String(value).replace(".", ",")} kg`

function searchPage(query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  const rows = words.length
    ? materials.filter((material) => {
        const text = `${material.number} ${material.name} ${material.group}`.toLowerCase()
        return words.every((word) => text.includes(word))
      })
    : []
  const result = !words.length
    ? html`<p class="notice">Bitte einen Suchbegriff eingeben (Kataloggruppe, Katalognummer oder Bezeichnung).</p>`
    : rows.length > MAX_HITS
      ? html`<p role="alert">Zu viele Treffer (${rows.length}). Bitte die Suche eingrenzen (höchstens ${MAX_HITS}).</p>`
      : !rows.length
        ? html`<p class="notice">Keine Treffer.</p>`
        : html`<p>${count(rows.length, "Treffer", "Treffer")}</p><table><thead><tr><th>Katalognummer</th><th>Bezeichnung</th><th>Kataloggruppe</th><th>Status</th></tr></thead><tbody>${rows.map(
            (material) =>
              html`<tr><td><a href="/materials/${material.number}">${material.number}</a></td><td>${material.name}</td><td>${material.group}</td><td>${material.status}</td></tr>`,
          )}</tbody></table>`
  return view(
    "Materialsuche",
    html`<form method="get" action="/"><label>Kataloggruppe, Katalognummer oder Bezeichnung <input name="q" value="${query}"></label><button type="submit">Suchen</button></form>${result}`,
  )
}

function materialPage(material) {
  return view(
    `${material.number} · ${material.name}`,
    html`<h2>Stammdaten</h2><dl><dt>Status</dt><dd>${material.status}</dd><dt>Kataloggruppe</dt><dd>${material.group}</dd>
    <dt>Bohrung</dt><dd>${material.bore} mm</dd><dt>Hub</dt><dd>${material.stroke} mm</dd><dt>Betriebsdruck</dt><dd>${material.pressure} bar</dd>
    <dt>Befestigung</dt><dd>${material.mounting}</dd><dt>Zertifizierung</dt><dd>${material.certification ?? "keine"}</dd>
    <dt>Zuschlagsmasse</dt><dd>${kg(material.surchargeMass)} je Stück</dd><dt>Metallindex</dt><dd>${material.metalIndex}</dd></dl>
    <h2>Bestand und Lieferzeiten</h2><dl><dt>Frei verwendbarer Bestand</dt><dd>${material.stock} Stk</dd>
    <dt>Gesperrter Bestand</dt><dd>${material.blockedStock} Stk</dd>
    <dt>Lieferzeit ab Lager</dt><dd>${count(material.stockWeeks, "Woche", "Wochen")}</dd>
    <dt>Fertigungszeit</dt><dd>${count(material.productionWeeks, "Woche", "Wochen")}</dd></dl>
    <h2>Preiskonditionen (netto)</h2>${material.conditions.map(
      (condition) =>
        html`<h3>Gültig vom ${germanDate(condition.from)} bis ${germanDate(condition.to)} (jeweils einschließlich)</h3>
        <p>Währung EUR · Preiseinheit ${condition.unit} Stk</p>
        <table><thead><tr><th>Staffel ab Menge</th><th>Preis je Preiseinheit</th></tr></thead><tbody>${condition.scales.map(
          (scale) => html`<tr><td>${scale.from} Stk</td><td>${money(scale.price)} EUR</td></tr>`,
        )}</tbody></table>`,
    )}<p><a href="/">Zurück zur Materialsuche</a></p>`,
  )
}

export async function erp(request, response) {
  const url = new URL(request.url, "http://erp")
  if (request.method === "GET" && url.pathname === "/")
    return send(response, 200, searchPage(url.searchParams.get("q") ?? ""))
  const number = /^\/materials\/([\w-]+)$/.exec(url.pathname)?.[1]
  const material = materials.find((item) => item.number === number)
  if (request.method === "GET" && material) return send(response, 200, materialPage(material))
  return send(response, 404, view("Material nicht gefunden", html`<p><a href="/">Zur Materialsuche</a></p>`))
}
