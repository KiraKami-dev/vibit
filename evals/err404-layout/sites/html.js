// What the four sites share: escaped HTML, the page layout (English, a cookie banner, a sidebar or top navigation),
// request helpers, per-run state, English number and date formats, and the other sites' addresses.
import { readFileSync } from "node:fs"

const manifest = JSON.parse(readFileSync(new URL("../atira-eval.json", import.meta.url), "utf8"))

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
export const html = (strings, ...values) =>
  new Raw(strings.reduce((text, part, index) => text + render(values[index - 1]) + part))

/** "1 week", "3 weeks" */
export const weeks = (n) => `${n} ${Number(n) === 1 ? "week" : "weeks"}`

const BANNER = `
(function () {
  try { if (document.cookie.indexOf('cookie_choice=') >= 0) return } catch (e) {}
  var shade = document.getElementById('cookie-shade')
  if (!shade) return
  shade.hidden = false
  shade.querySelector('[data-choice=necessary]').addEventListener('click', function () {
    document.cookie = 'cookie_choice=necessary; path=/; max-age=86400; SameSite=Lax'
    shade.remove()
  })
  shade.querySelector('[data-choice=settings]').addEventListener('click', function () {
    shade.querySelector('.cookie-more').hidden = false
  })
})()`

/**
 * A whole page: header with the site's name, navigation in a sidebar (side: true) or along the top, the title and the
 * content, and a cookie banner that covers the page until a choice is made.
 */
export function page({ site, title, nav = [], body, accent = "#24527a", signedIn = true, side = false }) {
  const links = nav.map(([href, text]) => html`<a href="${href}">${text}</a>`)
  return html`<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>${title} | ${site}</title>
<style>
  body{margin:0;font:14px/1.5 system-ui,sans-serif;color:#1d232a;background:#f7f7f4}
  header{background:${accent};color:#fff;padding:10px 20px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
  header strong{font-size:16px}header a{color:#fff}header form{margin-left:auto}header button{margin:0}
  .shell{display:flex;max-width:1180px;margin:18px auto;padding:0 12px;gap:18px}
  nav.side{flex:0 0 190px;display:flex;flex-direction:column;gap:6px;background:#fff;border:1px solid #d5d8dc;padding:12px;align-self:flex-start}
  main{flex:1;min-width:0;overflow-x:auto}
  table{border-collapse:collapse;width:100%;background:#fff}th,td{border:1px solid #d0d4d9;padding:5px 8px;text-align:left;vertical-align:top}
  th{background:#eceff2}label{margin:8px 0}input,select{font:inherit;padding:4px 6px}
  .field{display:block;margin:10px 0}.field input,.field select{min-width:240px}
  button{font:inherit;padding:5px 12px;margin:6px 10px 6px 0;cursor:pointer}
  .notice{background:#fff7d6;border:1px solid #c9b45d;padding:8px 12px}
  [role=alert]{border:2px solid #a40000;background:#fff;padding:8px 12px;white-space:pre-line}
  dl{display:grid;grid-template-columns:230px 1fr;background:#fff;border:1px solid #d0d4d9}dt,dd{margin:0;padding:5px 9px;border-bottom:1px solid #e5e7ea}
  dt{color:#4a5560}pre{white-space:pre-wrap;background:#fff;border:1px solid #d0d4d9;padding:12px;font:inherit}
  .tabs{display:flex;gap:4px;margin:8px 0 14px}.tabs a{padding:6px 12px;border:1px solid #c5cad0;background:#fff;text-decoration:none}
  .tabs a[aria-current=page]{background:${accent};color:#fff}
  #cookie-shade{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:flex-end;justify-content:center;z-index:50}
  #cookie-shade[hidden]{display:none}#cookie-shade div.box{background:#fff;max-width:640px;width:100%;padding:16px 20px;margin-bottom:30px}
  .combo{position:relative}.combo ul{position:absolute;z-index:5;left:0;top:100%;margin:0;padding:0;list-style:none;background:#fff;border:1px solid #888;max-height:220px;overflow:auto;min-width:330px}
  .combo li{padding:4px 8px;cursor:pointer}.combo li:hover,.combo li[aria-selected=true]{background:#dde7f3}
  fieldset{border:0;padding:0;margin:0}fieldset label{display:inline-block;margin:0 8px 0 0;white-space:nowrap}
</style></head><body><header><strong>${site}</strong>${side ? "" : links}${signedIn ? html`<form method="post" action="/logout"><button type="submit">Sign out</button></form>` : ""}</header>
<div class="shell">${side && links.length ? html`<nav class="side" aria-label="Sections">${links}</nav>` : ""}<main><h1>${title}</h1>${body}</main></div>
<div id="cookie-shade" hidden><div class="box" role="dialog" aria-label="Cookie settings"><p><strong>Cookies</strong></p>
<p>We use cookies that are needed to run this site. Optional analytics cookies help us improve it.</p>
<p class="cookie-more" hidden>Analytics cookies are currently unavailable.</p>
<button type="button" data-choice="settings">Settings</button><button type="button" data-choice="necessary">Accept necessary cookies only</button></div></div>
<script>${raw(BANNER)}</script></body></html>`.text
}

/** The sign-in page's content, with the challenge's field names (username, password, returnTo). */
export function loginPage(returnTo, error = "") {
  const back = typeof returnTo === "string" && returnTo.startsWith("/") && !returnTo.startsWith("//") ? returnTo : "/"
  return html`${error ? html`<p role="alert">${error}</p>` : ""}<form method="post" action="/login">
<input type="hidden" name="returnTo" value="${back}">
<label class="field">User name <input name="username" autocomplete="username" required></label>
<label class="field">Password <input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button></form>`
}

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

/** A run's own state, made on first use and dropped when the login expires. */
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

/** Money as the English sites write it: 1,234.56 */
export const money = (value) =>
  Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** A plain number in English format: 18.5, 1,310 */
export const number = (value) => Number(value).toLocaleString("en-US", { maximumFractionDigits: 4 })

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
/** A date as "16 Nov 2026" */
export const day = (value) => {
  const date = new Date(value)
  return `${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]} ${date.getUTCFullYear()}`
}
/** A time stamp as "16 Nov 2026, 09:00 UTC" */
export const stamp = (value) => {
  const date = new Date(value)
  return `${day(value)}, ${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")} UTC`
}

/**
 * An English number as typed into the bid form: "4.5" or "12"; with `isMoney`, exactly two decimals and optional
 * thousands commas ("1,234.56"). undefined when it isn't one.
 */
export function englishNumber(text, isMoney = false) {
  const value = String(text ?? "").trim()
  const pattern = isMoney ? /^(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)\.\d{2}$/ : /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/
  if (!pattern.test(value)) return undefined
  return Number(value.replaceAll(",", ""))
}
