// The buyer's supplier extranet: the technical specification with its revisions AND the bidder clarifications (Q&A).
// In the public scenario the Q&A sits on the tender portal; here it lives next to the specification. Nothing here
// changes during a run, so it keeps no per-run state.
import { clarifications, extranetNotices, specification, tender } from "../world.js"
import { day, html, page, send, stamp } from "./html.js"

const nav = [
  ["/", "Home"],
  ["/specifications", "Specifications"],
  ["/clarifications", "Clarifications"],
  ["/notices", "Notices"],
]
export const view = (title, body, signedIn) =>
  page({ site: `Supplier Extranet | ${tender.buyer.name}`, title, nav, body, accent: "#41315e", signedIn, side: true })

const PER_PAGE = 4

function requirementRow(r) {
  const stroke =
    r.usableStroke !== undefined
      ? html`usable stroke (nutzbarer Weg) ${r.usableStroke} mm; nominal stroke = usable stroke + 2 × end reserve ${r.reserveKey} (engineering release)`
      : html`${r.stroke} mm`
  return html`<tr><td>${r.ref}</td><td>${r.bore} mm</td><td>${stroke}</td><td>${r.pressure} bar</td><td>${r.mounting}</td>
    <td>${r.certification ?? "not required"}</td><td>${r.group}</td><td>${r.piecesPerSet ? `1 set = ${r.piecesPerSet} pcs` : "n/a"}</td></tr>`
}

function specificationPage(revision) {
  return view(
    `${specification.number}: ${specification.title}`,
    html`<form method="get" action="/specifications/${specification.number}"><label>Revision
      <select name="rev">${specification.revisions.map(
        (item) => html`<option value="${item.revision}" ${item.revision === revision.revision ? html`selected` : ""}>Rev. ${item.revision}</option>`,
      )}</select></label> <button type="submit">Show</button></form>
    <p>Showing Rev. ${revision.revision}, issued ${day(revision.date)}. Tender: ${tender.reference}.</p>
    <table><thead><tr><th>Requirement</th><th>Bore</th><th>Stroke</th><th>Operating pressure</th><th>Mounting</th>
      <th>Certification</th><th>Catalogue group (Kataloggruppe)</th><th>Set definition</th></tr></thead>
    <tbody>${revision.requirements.map(requirementRow)}</tbody></table>
    <details><summary>Revision history</summary><table><thead><tr><th>Revision</th><th>Issued</th><th>Status</th><th>Change note</th></tr></thead>
    <tbody>${specification.revisions.map(
      (item) =>
        html`<tr><td><a href="/specifications/${specification.number}?rev=${item.revision}">Rev. ${item.revision}</a></td><td>${day(item.date)}</td><td>${item.status}</td><td>${item.note}</td></tr>`,
    )}</tbody></table></details>`,
  )
}

function clarificationsPage(pageNo) {
  const sorted = [...clarifications].sort((a, b) => b.number.localeCompare(a.number))
  const pages = Math.ceil(sorted.length / PER_PAGE)
  const current = Math.min(Math.max(1, pageNo), pages)
  const rows = sorted.slice((current - 1) * PER_PAGE, current * PER_PAGE)
  return view(
    `Clarifications for ${tender.reference}`,
    html`<p>Questions from bidders and the buyer's answers, newest first. Answers are binding for all bidders.</p>
    ${rows.map(
      (item) =>
        html`<article style="background:#fff;border:1px solid #d0d4d9;padding:10px 14px;margin:10px 0">
        <h2 style="font-size:15px;margin:0">${item.number} <small style="font-weight:normal">published ${stamp(item.date)}</small></h2>
        <p><strong>Question:</strong> ${item.question}</p><p><strong>Answer:</strong> ${item.answer}</p></article>`,
    )}
    <p>Page ${current} of ${pages}
      ${current > 1 ? html` · <a href="/clarifications?page=${current - 1}">Newer</a>` : ""}
      ${current < pages ? html` · <a href="/clarifications?page=${current + 1}">Older</a>` : ""}</p>`,
  )
}

export async function extranet(request, response) {
  const url = new URL(request.url, "http://buyer-extranet")
  const path = url.pathname
  if (request.method !== "GET") return send(response, 405, view("Not allowed", html`<p><a href="/">Home</a></p>`))
  if (path === "/")
    return send(
      response,
      200,
      view(
        "Welcome",
        html`<p>${tender.buyer.name} publishes technical documents and bidder clarifications for its open tenders here.</p>
        <ul><li>${tender.reference}: ${tender.title}</li></ul>`,
      ),
    )
  if (path === "/specifications")
    return send(
      response,
      200,
      view(
        "Specifications",
        html`<table><thead><tr><th>Document</th><th>Title</th><th>Tender</th></tr></thead><tbody>
        <tr><td>${specification.number}</td><td><a href="/specifications/${specification.number}">${specification.title}</a></td><td>${tender.reference}</td></tr>
        </tbody></table>`,
      ),
    )
  if (path === `/specifications/${specification.number}`) {
    const wanted = url.searchParams.get("rev")
    // without a choice the first issue is shown, as in many document systems
    const revision = specification.revisions.find((item) => item.revision === wanted) ?? specification.revisions[0]
    return send(response, 200, specificationPage(revision))
  }
  if (path === "/clarifications")
    return send(response, 200, clarificationsPage(Number(url.searchParams.get("page") ?? "1") || 1))
  if (path === "/notices")
    return send(
      response,
      200,
      view(
        "Notices",
        html`<ul>${extranetNotices.map((item) => html`<li><a href="/notices/${item.id}">${item.title}</a></li>`)}</ul>`,
      ),
    )
  const notice = extranetNotices.find((item) => path === `/notices/${item.id}`)
  if (notice) return send(response, 200, view(notice.title, html`<p>${notice.content}</p>`))
  return send(response, 404, view("Page not found", html`<p><a href="/">Home</a></p>`))
}
