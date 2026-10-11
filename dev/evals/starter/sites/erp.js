// The bidder's ERP: the material master with each material's attributes, status, stock, lead times and price scale.
import { bidder, materials } from "../world.js"
import { count, html, money, page, send, siteUrl } from "./html.js"

const nav = () => [
  ["/", "Materialstamm"],
  [`${siteUrl("mail")}/`, "Werkpost"],
]
export const view = (title, body, signedIn) =>
  page({ site: `ERP · ${bidder.name}`, title, nav: nav(), body, accent: "#5b4a2f", signedIn })

export async function erp(request, response) {
  const url = new URL(request.url, "http://erp")
  if (request.method === "GET" && url.pathname === "/") {
    const query = (url.searchParams.get("q") ?? "").toLowerCase()
    const rows = materials.filter((material) => `${material.number} ${material.name}`.toLowerCase().includes(query))
    return send(
      response,
      200,
      view(
        "Materialstamm",
        html`<form method="get" action="/"><label>Katalognummer oder Bezeichnung <input name="q" value="${url.searchParams.get("q") ?? ""}"></label><button type="submit">Suchen</button></form>
        <table><thead><tr><th>Katalognummer</th><th>Bezeichnung</th><th>Status</th></tr></thead><tbody>${rows.map(
          (material) =>
            html`<tr><td><a href="/materials/${material.number}">${material.number}</a></td><td>${material.name}</td><td>${material.status}</td></tr>`,
        )}</tbody></table>`,
      ),
    )
  }
  const number = /^\/materials\/([\w-]+)$/.exec(url.pathname)?.[1]
  const material = materials.find((item) => item.number === number)
  if (request.method === "GET" && material)
    return send(
      response,
      200,
      view(
        `${material.number} · ${material.name}`,
        html`<dl><dt>Status</dt><dd>${material.status}</dd><dt>Bohrung</dt><dd>${material.bore} mm</dd>
        <dt>Hub</dt><dd>${material.stroke} mm</dd><dt>Betriebsdruck</dt><dd>${material.pressure} bar</dd>
        <dt>Befestigung</dt><dd>${material.mounting}</dd><dt>Zertifizierung</dt><dd>${material.certification ?? "keine"}</dd>
        <dt>Bestand</dt><dd>${material.stock} Stk</dd><dt>Lieferzeit ab Lager</dt><dd>${count(material.stockWeeks, "Woche", "Wochen")}</dd>
        <dt>Fertigungszeit</dt><dd>${count(material.productionWeeks, "Woche", "Wochen")}</dd></dl>
        <h2>Preisstaffel (EUR netto je Stück)</h2><table><thead><tr><th>ab Menge</th><th>Preis</th></tr></thead><tbody>${material.prices.map(
          (scale) => html`<tr><td>${scale.from} Stk</td><td>${money(scale.price)}</td></tr>`,
        )}</tbody></table><p><a href="/">Zurück zum Materialstamm</a></p>`,
      ),
    )
  return send(response, 404, view("Material nicht gefunden", html`<p><a href="/">Zum Materialstamm</a></p>`))
}
