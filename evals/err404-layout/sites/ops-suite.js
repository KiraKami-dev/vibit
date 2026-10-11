// The bidder's operations suite (ERP), in English: a material search (at most 20 hits), and per material three tabs:
// General (attributes, status, carton, weights, metal index), Stock & lead times, and Pricing (conditions with
// validity, currency, FX key, price unit and scales).
import { bidder, groups, materials } from "../world.js"
import { day, html, money, number, page, send, siteUrl, weeks } from "./html.js"

const nav = () => [
  ["/", "Material search"],
  [`${siteUrl("inbox")}/`, "Mail"],
]
export const view = (title, body, signedIn) =>
  page({ site: `Operations Suite | ${bidder.name}`, title, nav: nav(), body, accent: "#2f5b4a", signedIn })

const MAX_HITS = 20
const label = (m) => `${groups[m.group]}: ${m.bore}/${m.stroke}, ${m.mounting}${m.certification ? `, ${m.certification}` : ""}`
const lead = ([value, unit]) => (unit === "weeks" ? weeks(value) : `${value} ${unit}`)

function search(query) {
  const q = query.trim().toLowerCase()
  const form = html`<form method="get" action="/"><label>Material number, catalogue group or description
    <input name="q" value="${query}"></label> <button type="submit">Search</button></form>`
  if (!q) return view("Material search", html`${form}<p>Enter a search term.</p>`)
  const words = q.split(/\s+/)
  const hits = materials.filter((m) =>
    words.every((w) => `${m.number} ${m.group} ${label(m)}`.toLowerCase().includes(w)),
  )
  if (hits.length > MAX_HITS)
    return view(
      "Material search",
      html`${form}<p class="notice">Your search matches more than ${MAX_HITS} materials. Refine it (for example with a catalogue group).</p>`,
    )
  return view(
    "Material search",
    html`${form}<p>${hits.length} result(s)</p><table><thead><tr><th>Material</th><th>Description</th><th>Catalogue group</th><th>Status</th></tr></thead>
    <tbody>${hits.map(
      (m) =>
        html`<tr><td><a href="/materials/${m.number}">${m.number}</a></td><td>${label(m)}</td><td>${m.group}</td><td>${m.status}</td></tr>`,
    )}</tbody></table>`,
  )
}

function materialPage(m, tab) {
  const tabs = [
    ["general", "General"],
    ["stock", "Stock & lead times"],
    ["pricing", "Pricing"],
  ]
  const bar = html`<div class="tabs" role="tablist">${tabs.map(
    ([key, text]) =>
      html`<a role="tab" href="/materials/${m.number}?tab=${key}" ${key === tab ? html`aria-current="page"` : ""}>${text}</a>`,
  )}</div>`
  let body
  if (tab === "stock")
    body = html`<dl><dt>Unrestricted stock</dt><dd>${m.stock} pcs</dd><dt>Blocked stock (quality inspection)</dt><dd>${m.blocked} pcs</dd>
      <dt>Total stock</dt><dd>${m.stock + m.blocked} pcs</dd>
      <dt>Lead time from stock</dt><dd>${lead(m.leadStock)}</dd><dt>Production lead time</dt><dd>${lead(m.leadProduction)}</dd></dl>
      <p><small>Working days are Monday to Friday; 5 working days make one week.</small></p>`
  else if (tab === "pricing")
    body = html`${m.conditions.map(
      (c, index) =>
        html`<section style="background:#fff;border:1px solid #d0d4d9;padding:8px 12px;margin:10px 0">
        <h2 style="font-size:15px;margin:4px 0">Sales condition ${index + 1}</h2>
        <dl><dt>Valid from</dt><dd>${day(c.from)}</dd><dt>Valid to</dt><dd>${day(c.to)}</dd><dt>Currency</dt><dd>${c.currency}</dd>
        ${c.fxKey ? html`<dt>FX key</dt><dd>${c.fxKey}</dd>` : ""}<dt>Price unit</dt><dd>per ${c.priceUnit} pcs</dd></dl>
        <table><thead><tr><th>From quantity (pcs)</th><th>Price (${c.currency} per ${c.priceUnit} pcs)</th></tr></thead><tbody>${[...c.scales]
          .sort((a, b) => b.min - a.min)
          .map((s) => html`<tr><td>${s.min}</td><td>${money(s.price)}</td></tr>`)}</tbody></table></section>`,
    )}<p><small>Validity dates are inclusive.</small></p>`
  else
    body = html`<dl><dt>Description</dt><dd>${label(m)}</dd><dt>Status</dt><dd>${m.status}</dd><dt>Catalogue group</dt><dd>${m.group}</dd>
      <dt>Bore</dt><dd>${m.bore} mm</dd><dt>Stroke</dt><dd>${m.stroke} mm</dd><dt>Operating pressure</dt><dd>${m.pressure} bar</dd>
      <dt>Mounting</dt><dd>${m.mounting}</dd><dt>Certification</dt><dd>${m.certification ?? "none"}</dd>
      <dt>Pieces per carton</dt><dd>${m.carton}</dd><dt>Surcharge weight (metal surcharge)</dt><dd>${number(m.surchargeKg)} kg per piece</dd>
      <dt>Allocation weight (setup pools)</dt><dd>${number(m.allocationKg)} kg per piece</dd><dt>Metal index</dt><dd>${m.index}</dd></dl>`
  return view(`${m.number}`, html`<p><a href="/">Back to the search</a></p>${bar}${body}`)
}

export async function opsSuite(request, response) {
  const url = new URL(request.url, "http://ops-suite")
  if (request.method === "GET" && url.pathname === "/")
    return send(response, 200, search(url.searchParams.get("q") ?? ""))
  const id = /^\/materials\/([\w-]+)$/.exec(url.pathname)?.[1]
  const material = materials.find((item) => item.number === id)
  if (request.method === "GET" && material)
    return send(response, 200, materialPage(material, url.searchParams.get("tab") ?? "general"))
  return send(response, 404, view("Material not found", html`<p><a href="/">Material search</a></p>`))
}
