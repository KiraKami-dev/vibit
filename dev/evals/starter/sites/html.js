// What the four sites share: escaped HTML, the page layout, request helpers, per-run state and the other sites'
// addresses.
import { readFileSync } from "node:fs"

const manifest = JSON.parse(readFileSync(new URL("../atira-eval.json", import.meta.url), "utf8"))

/** A site's name as the platform writes it in variable names: erp → ERP, tender-portal → TENDER_PORTAL. */
const variable = (name) => name.toUpperCase().replace(/[^A-Z0-9]/g, "_")

/** The port a site listens on: PORT_<NAME>, which the platform and `atira eval dev` set, else the manifest's. */
export function port(name) {
  const site = manifest.sites.find((candidate) => candidate.name === name)
  return Number(process.env[`PORT_${variable(name)}`] ?? site?.port)
}

/** Where the agent's browser reaches a site: ATIRA_SITE_URL_<NAME> (its public URL), else localhost. */
export function siteUrl(name) {
  return (process.env[`ATIRA_SITE_URL_${variable(name)}`] ?? `http://localhost:${port(name)}`).replace(/\/+$/, "")
}

class Raw {
  constructor(text) {
    this.text = text
  }
}
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`)
const render = (value) =>
  value === null || value === undefined || value === false
    ? ""
    : value instanceof Raw
      ? value.text
      : Array.isArray(value)
        ? value.map(render).join("")
        : escapeHtml(value)

/** Text that goes into the page as it is: only for markup this app wrote itself, never for input. */
export const raw = (text) => new Raw(text)

/** HTML with every value escaped, except other html`` results; arrays are joined. */
/** "1 Woche", "3 Wochen": a count and its noun, singular or plural. */
export const count = (n, one, many) => `${n} ${Number(n) === 1 ? one : many}`

export const html = (strings, ...values) =>
  new Raw(strings.reduce((text, part, index) => text + render(values[index - 1]) + part))

/** A whole page: the site's name in the header, its navigation (with "Abmelden"), the title and the content. */
export function page({ site, title, nav = [], body, accent = "#24527a", signedIn = true }) {
  return html`<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>${title} – ${site}</title>
<style>
  header form{margin-left:auto}header button{margin:0}
  body{margin:0;font:15px/1.5 system-ui,sans-serif;color:#1d232a;background:#f4f5f7}
  header{background:${accent};color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:center;flex-wrap:wrap}
  header strong{font-size:17px}header a{color:#fff}main{max-width:960px;margin:24px auto;padding:0 16px}
  main{overflow-x:auto}table{border-collapse:collapse;width:100%;background:#fff}th,td{border:1px solid #c8ced5;padding:6px 10px;text-align:left}
  th{background:#e7ebef}label{display:block;margin:14px 0}input,select{font:inherit;padding:5px 8px;min-width:260px}
  button{font:inherit;padding:6px 14px;margin:8px 12px 8px 0;cursor:pointer}
  .notice{background:#fff7d6;border:1px solid #c9b45d;padding:10px 14px}
  [role=alert]{border:2px solid #a40000;background:#fff;padding:10px 14px;white-space:pre-line}
  dl{display:grid;grid-template-columns:220px 1fr;background:#fff;border:1px solid #c8ced5}dt,dd{margin:0;padding:6px 10px;border-bottom:1px solid #e1e5e9}
  dt{color:#4a5560}pre{white-space:pre-wrap;background:#fff;border:1px solid #c8ced5;padding:14px;font:inherit}
</style></head><body><header><strong>${site}</strong>${nav.map(([href, text]) => html`<a href="${href}">${text}</a>`)}${signedIn ? html`<form method="post" action="/logout"><button type="submit">Abmelden</button></form>` : ""}</header>
<main><h1>${title}</h1>${body}</main></body></html>`.text
}

/** A request's body as text, at most 1 MB. */
export async function readBody(request) {
  const chunks = []
  let size = 0
  for await (const chunk of request) {
    size += chunk.length
    if (size > 1024 * 1024) throw new Error("request body too large")
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString("utf8")
}

/** A form post's fields. */
export const readForm = async (request) => Object.fromEntries(new URLSearchParams(await readBody(request)))

export function send(response, status, body, type = "text/html; charset=utf-8") {
  response.writeHead(status, { "content-type": type, "cache-control": "no-store" })
  response.end(body)
}

export const sendJson = (response, status, value) =>
  send(response, status, JSON.stringify(value), "application/json; charset=utf-8")

export function redirect(response, location) {
  response.writeHead(303, { location })
  response.end()
}

/**
 * A run's own state, made on first use: the app serves every run of this eval version at once, so whatever a run can
 * change is kept per login (its run or preview id) and dropped when the login expires.
 */
export function perRun(fresh) {
  const states = new Map()
  return (login) => {
    const now = Date.now()
    for (const [id, entry] of states) if (entry.expiresAt < now) states.delete(id)
    const key = `${login.kind}:${login.id}`
    if (!states.has(key)) states.set(key, { expiresAt: login.expiresAt, state: fresh() })
    return states.get(key).state
  }
}

/** A number as the German sites write money: 1.234,56 */
export const money = (value) => value.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** A date (YYYY-MM-DD or ISO time) as 05.10.2026 */
export const germanDate = (value) => new Date(value).toLocaleDateString("de-DE", { timeZone: "UTC" })

/**
 * A German number as typed into a form: "3,5" or "12"; with `money`, exactly two decimals and optional thousands
 * dots ("1.234,56"). undefined when it isn't one.
 */
export function germanNumber(text, isMoney = false) {
  const pattern = isMoney ? /^(?:0|[1-9]\d*|[1-9]\d{0,2}(?:\.\d{3})+),\d{2}$/ : /^(?:0|[1-9]\d*)(?:,\d{1,2})?$/
  if (!pattern.test(text ?? "")) return undefined
  return Number(text.replaceAll(".", "").replace(",", "."))
}
