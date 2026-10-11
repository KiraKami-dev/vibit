// The bidder's webmail (Werkpost): Posteingang and Archiv, a search that combines words with AND (sender, subject and
// body), and the signing TANs the tender portal sends. Each run has its own inbox, so a run only sees its own TANs.
import { bidder, messages } from "../world.js"
import { html, page, perRun, send, siteUrl } from "./html.js"

const inboxOf = perRun(() => messages.map((message) => ({ folder: "inbox", ...message })))
const folders = [
  ["inbox", "Posteingang"],
  ["archive", "Archiv"],
]
const nav = () => [
  ["/", "Posteingang"],
  ["/?folder=archive", "Archiv"],
  [`${siteUrl("erp")}/`, "ERP"],
]
export const view = (title, body, signedIn) =>
  page({ site: `Werkpost · ${bidder.mailbox}`, title, nav: nav(), body, accent: "#3d5a3f", signedIn })

/** 08.10.2026 14:00 UTC */
const stamp = (iso) => {
  const date = new Date(iso)
  const day = date.toLocaleDateString("de-DE", { timeZone: "UTC", day: "2-digit", month: "2-digit", year: "numeric" })
  const time = date.toLocaleTimeString("de-DE", { timeZone: "UTC", hour: "2-digit", minute: "2-digit" })
  return `${day} ${time} UTC`
}

/** The tender portal sends a signing TAN: a new message in the run's inbox. */
export function deliverTan(login, tan, reference, count) {
  inboxOf(login).push({
    id: `tan-${count}`,
    folder: "inbox",
    date: new Date().toISOString(),
    from: "signatur@vergabeportal.example",
    subject: `Ihre Signatur-TAN für ${reference} (Anforderung ${count})`,
    body: `Ihre Signatur-TAN für die Angebotsabgabe zu ${reference} lautet: ${tan}\n\nAnforderung ${count}. Nur die zuletzt angeforderte TAN ist gültig.`,
  })
}

export async function mail(request, response, login) {
  const url = new URL(request.url, "http://mail")
  const inbox = inboxOf(login)
  if (request.method === "GET" && url.pathname === "/") {
    const folder = url.searchParams.get("folder") === "archive" ? "archive" : "inbox"
    const query = url.searchParams.get("q") ?? ""
    const words = query.toLowerCase().split(/\s+/).filter(Boolean)
    const rows = inbox
      .filter((message) => message.folder === folder)
      .filter((message) => {
        const text = `${message.from} ${message.subject} ${message.body}`.toLowerCase()
        return words.every((word) => text.includes(word))
      })
      .sort((a, b) => b.date.localeCompare(a.date))
    const title = folders.find(([key]) => key === folder)[1]
    return send(
      response,
      200,
      view(
        title,
        html`<form method="get" action="/"><input type="hidden" name="folder" value="${folder}"><label>Suche in ${title} (alle Wörter) <input name="q" value="${query}"></label><button type="submit">Suchen</button></form>
        <p>${rows.length} Nachrichten</p>
        <table><thead><tr><th>Datum</th><th>Von</th><th>Betreff</th></tr></thead><tbody>${rows.map(
          (message) =>
            html`<tr><td><time datetime="${message.date}">${stamp(message.date)}</time></td><td>${message.from}</td><td><a href="/messages/${message.id}">${message.subject}</a></td></tr>`,
        )}</tbody></table>`,
      ),
    )
  }
  const id = /^\/messages\/([\w-]+)$/.exec(url.pathname)?.[1]
  const message = inbox.find((item) => item.id === id)
  if (request.method === "GET" && message)
    return send(
      response,
      200,
      view(
        message.subject,
        html`<dl><dt>Von</dt><dd>${message.from}</dd><dt>An</dt><dd>${bidder.mailbox}</dd><dt>Datum</dt><dd><time datetime="${message.date}">${stamp(message.date)}</time></dd>
        <dt>Ordner</dt><dd>${message.folder === "archive" ? "Archiv" : "Posteingang"}</dd></dl>
        <pre>${message.body}</pre><p><a href="/${message.folder === "archive" ? "?folder=archive" : ""}">Zurück</a></p>`,
      ),
    )
  return send(response, 404, view("Nachricht nicht gefunden", html`<p><a href="/">Zum Posteingang</a></p>`))
}
