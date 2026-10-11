// The buyer's customer portal (Lieferantenportal): its document register with the technical specification, each
// revision's requirements (with Kataloggruppe and the original wording) and the revision history. Nothing here
// changes, so it keeps no per-run state.
import { informationDocuments, specification, tender } from "../world.js"
import { germanDate, html, page, send } from "./html.js"

const nav = [
  ["/", "Startseite"],
  ["/documents", "Dokumente"],
]
export const view = (title, body, signedIn) =>
  page({ site: `Lieferantenportal · ${tender.buyer.name}`, title, nav, body, accent: "#6a2f4f", signedIn })

const current = () => specification.revisions.find((revision) => revision.status === "gültig")
const certification = (requirement) => requirement.certification ?? "keine gefordert"

/** The requirement in words, as the buyer wrote it. */
function wording(requirement, revision) {
  const stroke = requirement.usableStroke
    ? `einem nutzbaren Weg von ${requirement.usableStroke} mm (Endreserve gemäß Technikfreigabe, Schlüssel ${requirement.reserveKey}, Revision ${revision.revision})`
    : `einem Hub von ${requirement.stroke} mm`
  return `Anforderung ${requirement.ref} (Kataloggruppe ${requirement.group}): Hydraulikzylinder mit Bohrung ${requirement.bore} mm, ${stroke}, Betriebsdruck ${requirement.pressure} bar, Befestigung ${requirement.mounting}, Zertifizierung: ${certification(requirement)}.`
}

function documents() {
  return view(
    "Dokumente",
    html`<table><thead><tr><th>Dokumentnummer</th><th>Titel</th><th>Typ</th></tr></thead><tbody>
    ${informationDocuments.map(
      (document) =>
        html`<tr><td>${document.number}</td><td><a href="/documents/${document.id}">${document.title}</a></td><td>Allgemein</td></tr>`,
    )}
    <tr><td>${specification.number}</td><td><a href="/documents/specification">${specification.title}</a></td><td>Technische Spezifikation zu ${tender.reference}</td></tr>
    </tbody></table>`,
  )
}

function specificationPage(revision) {
  return view(
    `${specification.number} · ${specification.title}`,
    html`<p>Revision ${revision.revision} vom ${germanDate(revision.date)} · Status: ${revision.status}</p>
    <table><thead><tr><th>Referenz</th><th>Kataloggruppe</th><th>Bohrung</th><th>Hub</th><th>Druck</th><th>Befestigung</th><th>Zertifizierung</th></tr></thead><tbody>${revision.requirements.map(
      (requirement) =>
        html`<tr><td>${requirement.ref}</td><td>${requirement.group}</td><td>${requirement.bore} mm</td><td>${requirement.usableStroke ? `nutzbarer Weg ${requirement.usableStroke} mm` : `${requirement.stroke} mm`}</td><td>${requirement.pressure} bar</td><td>${requirement.mounting}</td><td>${certification(requirement)}</td></tr>`,
    )}</tbody></table>
    <h2>Originalwortlaut</h2>${revision.requirements.map((requirement) => html`<p>${wording(requirement, revision)}</p>`)}
    <h2>Revisionshistorie</h2><table><thead><tr><th>Revision</th><th>Datum</th><th>Status</th><th>Änderungsvermerk</th></tr></thead><tbody>${specification.revisions.map(
      (item) =>
        html`<tr><td><a href="/documents/specification?revision=${item.revision}">Revision ${item.revision}</a></td><td>${germanDate(item.date)}</td><td>${item.status}</td><td>${item.note}</td></tr>`,
    )}</tbody></table><p><a href="/documents">Zurück zu den Dokumenten</a></p>`,
  )
}

export async function customerPortal(request, response) {
  const url = new URL(request.url, "http://customer-portal")
  if (request.method !== "GET")
    return send(response, 405, view("Nicht erlaubt", html`<p><a href="/">Zur Startseite</a></p>`))
  if (url.pathname === "/")
    return send(
      response,
      200,
      view(
        "Willkommen im Lieferantenportal",
        html`<p>${tender.buyer.name} stellt Ihnen hier technische Unterlagen und allgemeine Informationen bereit.</p>
        <p><a href="/documents">Zu den Dokumenten</a></p>`,
      ),
    )
  if (url.pathname === "/documents") return send(response, 200, documents())
  if (url.pathname === "/documents/specification") {
    const wanted = url.searchParams.get("revision")
    const revision = wanted ? specification.revisions.find((item) => item.revision === wanted) : current()
    if (revision) return send(response, 200, specificationPage(revision))
  }
  const document = informationDocuments.find((item) => url.pathname === `/documents/${item.id}`)
  if (document)
    return send(
      response,
      200,
      view(
        `${document.number} · ${document.title}`,
        html`<p>${document.content}</p><p><a href="/documents">Zurück zu den Dokumenten</a></p>`,
      ),
    )
  return send(response, 404, view("Seite nicht gefunden", html`<p><a href="/">Zur Startseite</a></p>`))
}
