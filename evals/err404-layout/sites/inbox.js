// The bidder's mail: two folders (Inbox, Archive), each listed OLDEST first in pages of six, with a search box (all
// words must occur in sender, subject or text). The sourcing hub drops signing codes into the run's inbox with a
// function call; they are dated on the scenario's clock, so they sort among the other mails.
import { bidder, messages, scenarioDate } from "../world.js"
import { html, page, perRun, send, siteUrl, stamp } from "./html.js"

const boxOf = perRun(() => ({ mails: [...messages], started: Date.now() }))
const nav = () => [
  ["/?folder=inbox", "Inbox"],
  ["/?folder=archive", "Archive"],
  [`${siteUrl("ops-suite")}/`, "Operations Suite"],
]
export const view = (title, body, signedIn) =>
  page({ site: `Mail | ${bidder.mailbox}`, title, nav: nav(), body, accent: "#5b3d2f", signedIn, side: true })

const PER_PAGE = 6

/** The sourcing hub sends a signing code: a new mail in the run's inbox, dated on the scenario's clock. */
export function deliverTan(login, tan, reference, count) {
  const box = boxOf(login)
  box.mails.push({
    id: `code-${count}`,
    folder: "inbox",
    date: new Date(Date.parse(scenarioDate) + (Date.now() - box.started)).toISOString(),
    from: "no-reply@sourcing-hub.example",
    subject: `Signing code ${count} for your bid on ${reference}`,
    body: `Your one-time signing code for the bid on ${reference} is ${tan}\n\nOnly the most recently requested code is valid. It expires after five minutes.`,
  })
}

export async function inbox(request, response, login) {
  const url = new URL(request.url, "http://inbox")
  const box = boxOf(login)
  if (request.method === "GET" && url.pathname === "/") {
    const folder = url.searchParams.get("folder") === "archive" ? "archive" : "inbox"
    const query = (url.searchParams.get("q") ?? "").trim()
    const words = query.toLowerCase().split(/\s+/).filter(Boolean)
    const rows = box.mails
      .filter((m) => m.folder === folder)
      .filter((m) => words.every((w) => `${m.from} ${m.subject} ${m.body}`.toLowerCase().includes(w)))
      .sort((a, b) => a.date.localeCompare(b.date))
    const pages = Math.max(1, Math.ceil(rows.length / PER_PAGE))
    const current = Math.min(Math.max(1, Number(url.searchParams.get("page") ?? "1") || 1), pages)
    const link = (n) => `/?folder=${folder}&q=${encodeURIComponent(query)}&page=${n}`
    return send(
      response,
      200,
      view(
        folder === "inbox" ? "Inbox" : "Archive",
        html`<form method="get" action="/"><input type="hidden" name="folder" value="${folder}">
          <label>Search this folder <input name="q" value="${query}"></label> <button type="submit">Search</button></form>
        <p>${rows.length} message(s), oldest first${query ? html` matching all of: ${query}` : ""}.</p>
        <table><thead><tr><th>Received</th><th>From</th><th>Subject</th></tr></thead><tbody>${rows
          .slice((current - 1) * PER_PAGE, current * PER_PAGE)
          .map(
            (m) =>
              html`<tr><td>${stamp(m.date)}</td><td>${m.from}</td><td><a href="/messages/${m.id}">${m.subject}</a></td></tr>`,
          )}</tbody></table>
        <p>Page ${current} of ${pages}${current > 1 ? html` · <a href="${link(current - 1)}">Previous</a>` : ""}${current < pages ? html` · <a href="${link(current + 1)}">Next</a>` : ""}</p>`,
      ),
    )
  }
  const id = /^\/messages\/([\w-]+)$/.exec(url.pathname)?.[1]
  const message = box.mails.find((item) => item.id === id)
  if (request.method === "GET" && message)
    return send(
      response,
      200,
      view(
        message.subject,
        html`<dl><dt>From</dt><dd>${message.from}</dd><dt>To</dt><dd>${bidder.mailbox}</dd><dt>Received</dt><dd>${stamp(message.date)}</dd>
        <dt>Folder</dt><dd>${message.folder === "inbox" ? "Inbox" : "Archive"}</dd></dl><pre>${message.body}</pre>
        <p><a href="/?folder=${message.folder}">Back to the folder</a></p>`,
      ),
    )
  return send(response, 404, view("Message not found", html`<p><a href="/">Inbox</a></p>`))
}
