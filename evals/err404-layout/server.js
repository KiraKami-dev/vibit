// Starts the eval's four sites, each on its own port: PORT_<NAME> when the platform (or `atira eval dev`) sets it,
// else the port in atira-eval.json. Every page needs the run's login for its site (atira-kit.js checks it with the
// platform); the handlers get that login, so each run keeps its own state in the one shared app.
import { createServer } from "node:http"
import { guard } from "./atira-kit.js"
import { extranet, view as extranetView } from "./sites/buyer-extranet.js"
import { loginPage, port, raw, send, siteUrl } from "./sites/html.js"
import { inbox, view as inboxView } from "./sites/inbox.js"
import { opsSuite, view as opsView } from "./sites/ops-suite.js"
import { sourcingHub, view as hubView } from "./sites/sourcing-hub.js"

const sites = {
  "sourcing-hub": [sourcingHub, hubView],
  "buyer-extranet": [extranet, extranetView],
  "ops-suite": [opsSuite, opsView],
  inbox: [inbox, inboxView],
}

for (const [name, [handle, view]] of Object.entries(sites)) {
  const layout = (title, body) => view(title === "Anmeldung" ? "Sign in" : title, raw(body), false)
  createServer(async (request, response) => {
    try {
      const url = new URL(request.url ?? "/", "http://site")
      // the sign-in form in English; its POST (and the session) is the kit's
      if (request.method === "GET" && url.pathname === "/login")
        return send(response, 200, view("Sign in", loginPage(url.searchParams.get("returnTo") ?? "/"), false))
      const login = await guard(request, response, name, { layout })
      if (login) await handle(request, response, login)
    } catch (error) {
      console.error(`${name}: ${request.method} ${request.url}:`, error)
      if (!response.headersSent) response.writeHead(500, { "content-type": "text/plain; charset=utf-8" })
      response.end("Internal error")
    }
  }).listen(port(name), () => console.log(`${name} listening on port ${port(name)}, reached at ${siteUrl(name)}`))
}
