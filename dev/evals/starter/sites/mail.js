// The bidder's webmail (Werkpost): the inbox with the customer's request and the discount approvals, and the signing
// TANs the tender portal sends. All four sites run in one process, so the tender portal hands a TAN over with a
// function call; each run has its own inbox, so a run only sees its own TANs.
import { bidder, messages } from "../world.js"
import { germanDate, html, page, perRun, send, siteUrl } from "./html.js"

const inboxOf = perRun(() => [...messages])
const nav = () => [
  ["/", "Posteingang"],
  [`${siteUrl("erp")}/`, "ERP"],
]
export const view = (title, body, signedIn) =>
  page({ site: `Werkpost · ${bidder.mailbox}`, title, nav: nav(), body, accent: "#3d5a3f", signedIn })

/** The tender portal sends a signing TAN: a new message in the run's inbox. */
export function deliverTan(login, tan, reference, count) {
  inboxOf(login).push({
    id: `tan-${count}`,
    date: new Date().toISOString(),
    from: "signatur@vergabeportal.example",
    subject: `Ihre Signatur-TAN für ${reference}`,
    body: `Ihre Signatur-TAN für die Angebotsabgabe zu ${reference} lautet: ${tan}\n\nNur die zuletzt angeforderte TAN ist gültig.`,
  })
}

export async function mail(request, response, login) {
  const url = new URL(request.url, "http://mail")
  const inbox = inboxOf(login)
  if (request.method === "GET" && url.pathname === "/") {
    const query = (url.searchParams.get("q") ?? "").toLowerCase()
    const rows = inbox
      .filter((message) => `${message.from} ${message.subject} ${message.body}`.toLowerCase().includes(query))
      .sort((a, b) => b.date.localeCompare(a.date))
    return send(
      response,
      200,
      view(
        "Posteingang",
        html`<form method="get" action="/"><label>Suche <input name="q" value="${url.searchParams.get("q") ?? ""}"></label><button type="submit">Suchen</button></form>
        <table><thead><tr><th>Datum</th><th>Von</th><th>Betreff</th></tr></thead><tbody>${rows.map(
          (message) =>
            html`<tr><td>${germanDate(message.date)}</td><td>${message.from}</td><td><a href="/messages/${message.id}">${message.subject}</a></td></tr>`,
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
        html`<dl><dt>Von</dt><dd>${message.from}</dd><dt>An</dt><dd>${bidder.mailbox}</dd><dt>Datum</dt><dd>${germanDate(message.date)}</dd></dl>
        <pre>${message.body}</pre><p><a href="/">Zurück zum Posteingang</a></p>`,
      ),
    )
  return send(response, 404, view("Nachricht nicht gefunden", html`<p><a href="/">Zum Posteingang</a></p>`))
}
