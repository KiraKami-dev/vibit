// atira-kit.js: the login, the session and the answer for an eval app on the EHL Challenge platform. Plain Node 24,
// no dependencies: copy this file into your own app (plain Node, Express, Next.js) and keep it as it is.
//
// The platform starts the app with ATIRA_API_URL and ATIRA_EVAL_TOKEN. Each run's agent gets a login per site
// (username u_… for a run, p_… for a preview, and a password); the app checks it with POST
// {ATIRA_API_URL}/v1/eval-worlds/verify, keeps a signed cookie per site (asking again at most every 10 s, so a run that
// ended is signed out), and hands the run's answer in with POST {ATIRA_API_URL}/v1/eval-worlds/answer.
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto"

const apiUrl = () => (process.env.ATIRA_API_URL ?? "").replace(/\/+$/, "")
// the cookie key comes from the eval token, so every process of the app (and a restart) accepts the same cookies
const fallbackKey = randomBytes(32)
const cookieKey = () =>
  process.env.ATIRA_EVAL_TOKEN
    ? createHmac("sha256", process.env.ATIRA_EVAL_TOKEN).update("atira-kit session").digest()
    : fallbackKey

/** A site's name as the platform writes it in variable names and cookies: portal → PORTAL, my-site → MY_SITE. */
const variable = (name) => name.toUpperCase().replace(/[^A-Z0-9]/g, "_")
const cookieName = (site) => `atira_${variable(site).toLowerCase()}`

/** POSTs JSON to the platform with the eval's bearer token: {status, body, retryAfter} (seconds, on a 429). */
async function platform(path, payload) {
  if (!apiUrl()) throw new Error("ATIRA_API_URL is not set: start the app with atira eval dev, or on the platform")
  const response = await fetch(`${apiUrl()}${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${process.env.ATIRA_EVAL_TOKEN ?? ""}`, "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(20_000),
  })
  const retryAfter = Number(response.headers.get("retry-after")) || undefined
  return { status: response.status, body: await response.json().catch(() => ({})), retryAfter }
}

/**
 * What the platform says of a login: {login} when it is valid, {refused: true} for a 401, {limited: true, retryAfter}
 * for a 429, else {} (the platform away: try later).
 */
async function check(site, username, password) {
  if (!username || !password) return { refused: true }
  const { status, body, retryAfter } = await platform("/v1/eval-worlds/verify", { site, username, password })
  if (status === 401 || status === 403) return { refused: true }
  if (status === 429) return { limited: true, retryAfter: retryAfter ?? 10 }
  if (status !== 200) return {}
  const kind = body.run_id ? "run" : body.preview_id ? "preview" : null
  if (!kind) return { refused: true }
  const login = { kind, id: String(body.run_id ?? body.preview_id), expiresAt: Number(body.expires_at) }
  if (!(login.expiresAt > Date.now())) login.expiresAt = Date.now() + 3600_000
  return { login }
}

/**
 * Checks a login with the platform: the login ({kind: "run" | "preview", id, expiresAt}) for a run's or a preview's
 * username and password on this site, else null.
 */
export async function verifyLogin(site, username, password) {
  return (await check(site, username, password)).login ?? null
}

/**
 * Hands the login's answer (the bid) to the platform: {status, body, retryAfter}. 202 {accepted} for a run (scored afterwards; the
 * run ends), 200 {accepted, score: {quality, eligible, breakdown}} for a preview, 400 {issues} for a bid that doesn't
 * match the answer schema, 409 once the run has its answer or the preview is over.
 */
export function submitAnswer(login, bid) {
  return platform("/v1/eval-worlds/answer", { [login.kind === "run" ? "run_id" : "preview_id"]: login.id, bid })
}

const sign = (text) => createHmac("sha256", cookieKey()).update(text).digest("base64url")

/** The signed-in sessions: what the platform said of each, and the login to ask it again with. */
const sessions = new Map()
const RECHECK_MS = 10_000

/**
 * What the app tells the browser after handing in an answer: {status, body, headers}. The platform's body passes
 * through for an accepted answer (202 for a run, 200 with the score for a preview) and for 400 {issues}; every other
 * status gets an `error` the page can show, and a 429 keeps its retry-after.
 */
export function answerReply(login, answer) {
  const { status, body, retryAfter } = answer
  if (status < 300 || status === 400) return { status, body, headers: {} }
  const expired = body?.reason === "expired" || login.expiresAt < Date.now()
  if (status === 409)
    return {
      status: 409,
      body: {
        reason: expired ? "expired" : "closed",
        error: expired
          ? "Die Bearbeitungszeit ist abgelaufen. Eine Abgabe ist nicht mehr möglich."
          : login.kind === "run"
            ? "Für diesen Lauf wurde bereits ein Angebot abgegeben, oder der Lauf ist beendet."
            : "Die Vorschau ist nicht mehr aktiv.",
      },
      headers: {},
    }
  if (status === 401 || status === 403)
    return { status: 401, body: { error: "Ihre Sitzung ist nicht mehr gültig. Bitte erneut anmelden." }, headers: {} }
  if (status === 429) {
    const seconds = retryAfter ?? 10
    return {
      status: 429,
      body: { error: `Zu viele Abgaben in kurzer Zeit. Bitte in ${seconds} Sekunden erneut versuchen.` },
      headers: { "retry-after": String(seconds) },
    }
  }
  return {
    status: 502,
    body: { error: `Die Abgabe konnte nicht verarbeitet werden (HTTP ${status}). Bitte erneut versuchen.` },
    headers: {},
  }
}

/**
 * Signs `username` in on `site` when the platform accepts it: {login, cookie} with the Set-Cookie header that keeps
 * the session (signed, HttpOnly, until the login expires); {refused: true} for a wrong login; {limited: true,
 * retryAfter} when the platform says to slow down; {unavailable: true} when it doesn't answer.
 */
export async function signIn(site, username, password, secure = false) {
  const checked = await check(site, username, password).catch(() => ({}))
  if (checked.refused || checked.limited) return checked
  const login = checked.login
  if (!login) return { unavailable: true }
  const sid = randomBytes(16).toString("base64url")
  const now = Date.now()
  for (const [key, entry] of sessions) if (entry.login.expiresAt < now) sessions.delete(key)
  sessions.set(sid, { site, username, password, login, checked: now })
  return { login, cookie: sessionCookie(site, { ...login, sid }, secure) }
}

/** The Set-Cookie header that keeps `login` (with its session id `sid`) on `site`. */
export function sessionCookie(site, login, secure = false) {
  const payload = Buffer.from(
    JSON.stringify({ site, sid: login.sid, kind: login.kind, id: login.id, exp: login.expiresAt }),
  ).toString("base64url")
  const maxAge = Math.max(0, Math.floor((login.expiresAt - Date.now()) / 1000))
  return `${cookieName(site)}=${payload}.${sign(payload)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure ? "; Secure" : ""}`
}

/** The Set-Cookie header that ends the session on `site`. */
export const clearCookie = (site) => `${cookieName(site)}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`

/** The login a request's cookie holds for `site` ({sid, kind, id, expiresAt}), or null: missing, forged or expired. */
export function readLogin(cookieHeader, site) {
  const value = new RegExp(`(?:^|;\\s*)${cookieName(site)}=([A-Za-z0-9_-]+)\\.([A-Za-z0-9_-]+)`).exec(
    cookieHeader ?? "",
  )
  if (!value) return null
  const expected = Buffer.from(sign(value[1]))
  const given = Buffer.from(value[2])
  if (expected.length !== given.length || !timingSafeEqual(expected, given)) return null
  try {
    const data = JSON.parse(Buffer.from(value[1], "base64url").toString("utf8"))
    if (data.site !== site || !(data.exp > Date.now())) return null
    return { sid: data.sid, kind: data.kind, id: String(data.id), expiresAt: data.exp }
  } catch {
    return null
  }
}

/**
 * The login of a request on `site`, asking the platform again when it last did more than 10 s ago: null when there is
 * none, it was refused since (the run ended), or the app restarted since the sign-in (sign in again).
 */
export async function currentLogin(cookieHeader, site) {
  const cookie = readLogin(cookieHeader, site)
  const entry = cookie && sessions.get(cookie.sid)
  if (!entry || entry.site !== site) return null
  if (Date.now() - entry.checked > RECHECK_MS) {
    const again = await check(site, entry.username, entry.password).catch(() => ({}))
    if (again.refused) {
      sessions.delete(cookie.sid)
      return null
    }
    // the platform busy (429) or away: the session stays, and is asked about on the next request
    if (again.login) Object.assign(entry, { login: again.login, checked: Date.now() })
  }
  return entry.login
}

/** Signs a session out: the Set-Cookie header that ends it. */
export function signOut(cookieHeader, site) {
  const cookie = readLogin(cookieHeader, site)
  if (cookie) sessions.delete(cookie.sid)
  return clearCookie(site)
}

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`)

/** Only a path on this site is a place to return to after the login. */
const safeReturn = (value) => {
  if (typeof value !== "string" || !value.startsWith("/") || /[\\\s]/.test(value)) return "/"
  try {
    const url = new URL(value, "http://site.invalid")
    return url.origin === "http://site.invalid" ? url.pathname + url.search + url.hash : "/"
  } catch {
    return "/"
  }
}

/** The login form's content, with the main challenge's field names (username, password, returnTo). */
export function loginForm(returnTo = "/", error = "") {
  return `${error ? `<p role="alert">${escapeHtml(error)}</p>` : ""}<form method="post" action="/login">
<input type="hidden" name="returnTo" value="${escapeHtml(safeReturn(returnTo))}">
<label>Benutzername <input name="username" autocomplete="username" required></label>
<label>Passwort <input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Anmelden</button></form>`
}

const plainLayout = (title, body) =>
  `<!doctype html><html lang="de"><head><meta charset="utf-8"><title>${escapeHtml(title)}</title></head><body><h1>${escapeHtml(title)}</h1>${body}</body></html>`

async function formFields(request) {
  const chunks = []
  let size = 0
  for await (const chunk of request) {
    size += chunk.length
    if (size > 64 * 1024) break
    chunks.push(chunk)
  }
  return Object.fromEntries(new URLSearchParams(Buffer.concat(chunks).toString("utf8")))
}

/**
 * For a plain Node server (and Express: `app.use(async (req, res, next) => (req.login = await guard(req, res, "portal")) && next())`):
 * answers /login (the form, and its POST checked with the platform) and /logout itself, and sends every other page to
 * the login until the browser has signed in. Returns the request's login when the page may be served, else null (the
 * response is sent). `layout(title, bodyHtml)` puts the form into the site's own page.
 */
export async function guard(request, response, site, { layout = plainLayout, secure } = {}) {
  const url = new URL(request.url ?? "/", "http://site")
  const https = secure ?? /^https:/i.test(process.env[`ATIRA_SITE_URL_${variable(site)}`] ?? "")
  const html = (status, body, headers = {}) => {
    response.writeHead(status, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", ...headers })
    response.end(body)
  }
  const redirect = (location, cookie) => {
    response.writeHead(303, { location, "cache-control": "no-store", ...(cookie ? { "set-cookie": cookie } : {}) })
    response.end()
  }
  if (url.pathname === "/login" && request.method === "POST") {
    const fields = await formFields(request)
    const signed = await signIn(site, fields.username, fields.password, https)
    if (signed.login) redirect(safeReturn(fields.returnTo), signed.cookie)
    else if (signed.limited)
      html(
        429,
        layout(
          "Anmeldung",
          loginForm(
            fields.returnTo,
            `Zu viele Anmeldeversuche. Bitte in ${signed.retryAfter} Sekunden erneut versuchen.`,
          ),
        ),
        { "retry-after": String(signed.retryAfter) },
      )
    else if (signed.unavailable)
      html(
        503,
        layout(
          "Anmeldung",
          loginForm(fields.returnTo, "Die Anmeldung ist gerade nicht möglich. Bitte erneut versuchen."),
        ),
      )
    else html(401, layout("Anmeldung", loginForm(fields.returnTo, "Benutzername oder Passwort ist falsch.")))
    return null
  }
  if (url.pathname === "/login") {
    html(200, layout("Anmeldung", loginForm(url.searchParams.get("returnTo"))))
    return null
  }
  if (url.pathname === "/logout") {
    redirect("/login", signOut(request.headers.cookie, site))
    return null
  }
  const login = await currentLogin(request.headers.cookie, site)
  if (login) return login
  if (request.method === "GET" || request.method === "HEAD") {
    redirect(`/login?returnTo=${encodeURIComponent(url.pathname + url.search)}`)
    return null
  }
  response.writeHead(401, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" })
  response.end(JSON.stringify({ error: "Bitte zuerst anmelden." }))
  return null
}
