// Starts the eval's four sites, each on its own port: PORT_<NAME> when the platform (or `atira eval dev`) sets it,
// else the port in atira-eval.json. Every page needs the run's login for its site (atira-kit.js checks it with the
// platform); the handlers get that login, so each run keeps its own state in the one shared app.
import { createServer } from "node:http"
import { guard } from "./atira-kit.js"
import { customerPortal, view as customerPortalView } from "./sites/customer-portal.js"
import { erp, view as erpView } from "./sites/erp.js"
import { port, raw, siteUrl } from "./sites/html.js"
import { mail, view as mailView } from "./sites/mail.js"
import { tenderPortal, view as tenderPortalView } from "./sites/tender-portal.js"

const sites = {
  "tender-portal": [tenderPortal, tenderPortalView],
  "customer-portal": [customerPortal, customerPortalView],
  erp: [erp, erpView],
  mail: [mail, mailView],
}

for (const [name, [handle, view]] of Object.entries(sites)) {
  const layout = (title, body) => view(title, raw(body), false)
  createServer(async (request, response) => {
    try {
      const login = await guard(request, response, name, { layout })
      if (login) await handle(request, response, login)
    } catch (error) {
      console.error(`${name}: ${request.method} ${request.url}:`, error)
      if (!response.headersSent) response.writeHead(500, { "content-type": "text/plain; charset=utf-8" })
      response.end("Interner Fehler")
    }
  }).listen(port(name), () => console.log(`${name} listening on port ${port(name)}, reached at ${siteUrl(name)}`))
}
