// err404-nobid: a scenario whose one correct answer is no_bid. All names, numbers and references are invented. The
// derivation of the expected answer is in DERIVATION.md; change both together.
//
// Position 3 matches an active ERP material only before a scoped amendment in the tender Q&A (Revision C, S-3:
// 280 bar); after it, its only exact candidate is blocked, and look-alikes differ in one attribute. An amendment to
// the superseded Revision B would restore the old pressure but does not apply. Position 2 is a "nutzbarer Weg"
// requirement whose derivation (amendment, then twice the per-end reserve of the Revision C Technikfreigabe) matches,
// and whose wrong derivations match other active materials, so only position 3 decides. Mail: a superseded
// unconditional approval, a newer conditional approval (0 % for no_bid), a future-dated unconditional approval, a
// look-alike reference, and Technikfreigabe records for another revision, from a wrong domain and future-dated.

export const scenarioDate = "2026-10-09T10:00:00Z"

export const bidder = {
  name: "Beispiel Hydraulik GmbH",
  vatId: "DE999000123",
  mailbox: "vertrieb@beispiel-hydraulik.example",
}

export const tender = {
  reference: "VG-2026-0923",
  title: "Hydraulikzylinder für das Pumpwerk Beispieltal",
  buyer: { name: "Zweckverband Beispieltal", street: "Talstraße 5", postalCode: "54321", city: "Beispieltal" },
  procedure: "Offenes Verfahren",
  publishedAt: "2026-09-18",
  deadline: "2026-11-02T10:00:00Z",
  currency: "EUR",
  positions: [
    { position: "1", text: "Zylinder Schieber Zulauf, Flanschbefestigung", quantity: 8, unit: "Stk", requirementRef: "S-1" },
    { position: "2", text: "Zylinder Rückspülklappe, Gelenkauge", quantity: 4, unit: "Stk", requirementRef: "S-2" },
    { position: "3", text: "Zylinder Hauptschütz, Fußbefestigung", quantity: 6, unit: "Stk", requirementRef: "S-3" },
  ],
}

export const questions = [
  {
    number: 1,
    date: "2026-09-28T09:00:00Z",
    question: "Bleibt es bei Position 1 bei 8 Stück?",
    answer: "Nein, für Position 1 werden 10 Stk benötigt.",
  },
  {
    number: 2,
    date: "2026-10-02T08:00:00Z",
    question: "Gibt es technische Änderungen an Anforderung S-3?",
    answer:
      "Technische Änderung zu TS-0923 Revision C, Anforderung S-3: Betriebsdruck 280 bar statt 250 bar. Alle übrigen Merkmale bleiben unverändert.",
  },
  {
    number: 3,
    date: "2026-10-03T12:30:00Z",
    question: "Ist der nutzbare Weg für S-2 final?",
    answer:
      "Technische Änderung zu TS-0923 Revision C, Anforderung S-2: nutzbarer Weg 310 mm statt 300 mm. Alle übrigen Merkmale bleiben unverändert.",
  },
  {
    number: 4,
    date: "2026-10-04T14:00:00Z",
    question: "Gilt für S-3 in Revision B weiterhin der höhere Druck?",
    answer: "Technische Änderung zu TS-0923 Revision B, Anforderung S-3: Betriebsdruck 250 bar statt 280 bar.",
  },
  {
    number: 5,
    date: "2026-10-05T07:45:00Z",
    question: "Wohin ist zu liefern?",
    answer: "Lieferung frei Baustelle Pumpwerk Beispieltal, Talstraße 5.",
  },
]

export const specification = {
  number: "TS-0923",
  title: "Technische Spezifikation Hydraulikzylinder Pumpwerk Beispieltal",
  revisions: [
    {
      revision: "B",
      date: "2026-09-12",
      status: "ersetzt",
      note: "Zweitausgabe",
      requirements: [
        { ref: "S-1", group: "KG-FL40", bore: 40, stroke: 200, pressure: 160, mounting: "Flansch", certification: null },
        { ref: "S-2", group: "KG-GA63", bore: 63, usableStroke: 300, reserveKey: "ER-63", pressure: 210, mounting: "Gelenkauge", certification: null },
        { ref: "S-3", group: "KG-FU80", bore: 80, stroke: 400, pressure: 280, mounting: "Fuß", certification: null },
      ],
    },
    {
      revision: "C",
      date: "2026-09-26",
      status: "gültig",
      note: "S-3: Betriebsdruck 250 statt 280 bar",
      requirements: [
        { ref: "S-1", group: "KG-FL40", bore: 40, stroke: 200, pressure: 160, mounting: "Flansch", certification: null },
        { ref: "S-2", group: "KG-GA63", bore: 63, usableStroke: 300, reserveKey: "ER-63", pressure: 210, mounting: "Gelenkauge", certification: null },
        { ref: "S-3", group: "KG-FU80", bore: 80, stroke: 400, pressure: 250, mounting: "Fuß", certification: null },
      ],
    },
  ],
}

export const informationDocuments = [
  {
    id: "lieferantenleitfaden",
    number: "INF-001",
    title: "Leitfaden für Lieferanten",
    content: "Angebote geben Sie ausschließlich über das Vergabeportal ab. Technische Änderungen veröffentlichen wir in den Bieterfragen.",
  },
]

export const otherTenders = [
  { reference: "VG-2026-0923-B", title: "Hydraulikzylinder Pumpwerk Beispieltal, Los B", buyer: "Zweckverband Beispieltal", deadline: "2026-11-16T10:00:00Z" },
  { reference: "VG-2026-0932", title: "Armaturen Hochbehälter Nord", buyer: "Zweckverband Beispieltal", deadline: "2026-11-09T10:00:00Z" },
]

const L = "beispiel-hydraulik.example"

export const messages = [
  {
    id: "m-3001",
    folder: "inbox",
    date: "2026-09-19T08:00:00Z",
    from: "vergabe@zv-beispieltal.example",
    subject: "Ausschreibung VG-2026-0923 veröffentlicht",
    body: "Sehr geehrte Damen und Herren,\n\ndie Ausschreibung VG-2026-0923 ist im Vergabeportal veröffentlicht. Technische Änderungen veröffentlichen wir in den Bieterfragen.\n\nMit freundlichen Grüßen\nVergabestelle Zweckverband Beispieltal",
  },
  {
    id: "m-3002",
    folder: "archive",
    date: "2026-09-25T10:30:00Z",
    from: `leitung@${L}`,
    subject: "Rabattfreigabe VG-2026-0923",
    body: "Hallo Vertrieb,\n\nfür VG-2026-0923 genehmige ich 3 % Rabatt auf alle Positionen.\n\nViele Grüße\nAnna Beispiel\nLeitung Vertrieb",
  },
  {
    id: "m-3003",
    folder: "inbox",
    date: "2026-10-06T13:00:00Z",
    from: `leitung@${L}`,
    subject: "Rabattfreigabe VG-2026-0923 (ersetzt Freigabe vom 25.09.)",
    body: "Hallo Vertrieb,\n\nmeine Freigabe vom 25.09. für VG-2026-0923 ersetze ich: 4 % Rabatt, sofern der Nettoauftragswert vor Rabatt (Staffelpreise, ohne Zuschläge und Rüstkosten) mindestens 20.000,00 EUR beträgt. Andernfalls gibt es keinen Rabatt.\n\nViele Grüße\nAnna Beispiel\nLeitung Vertrieb",
  },
  {
    id: "m-3004",
    folder: "inbox",
    date: "2026-10-07T09:00:00Z",
    from: `leitung@${L}`,
    subject: "Rabattfreigabe VG-2026-0923-B",
    body: "Hallo Vertrieb,\n\nfür VG-2026-0923-B (Los B) genehmige ich 6 % Rabatt.\n\nViele Grüße\nAnna Beispiel\nLeitung Vertrieb",
  },
  {
    id: "m-3005",
    folder: "inbox",
    date: "2026-10-12T08:00:00Z",
    from: `leitung@${L}`,
    subject: "Rabattfreigabe VG-2026-0923 Nachtrag",
    body: "Hallo Vertrieb,\n\nNachtrag zu VG-2026-0923: 5 % Rabatt ohne Mindestauftragswert.\n\nViele Grüße\nAnna Beispiel\nLeitung Vertrieb",
  },
  {
    id: "m-3010",
    folder: "archive",
    date: "2026-09-20T11:00:00Z",
    from: `technik@${L}`,
    subject: "Technikfreigabe VG-2026-0923 Revision B",
    body: "Hallo Vertrieb,\n\nTechnikfreigabe zu VG-2026-0923, Spezifikation TS-0923 Revision B: Endreserve ER-63 beträgt 12 mm je Ende.\n\nGruß\nTechnik",
  },
  {
    id: "m-3011",
    folder: "inbox",
    date: "2026-10-01T15:00:00Z",
    from: `technik@${L}`,
    subject: "Technikfreigabe VG-2026-0923 Revision C",
    body: "Hallo Vertrieb,\n\nTechnikfreigabe zu VG-2026-0923, Spezifikation TS-0923 Revision C: Endreserve ER-63 beträgt 10 mm je Ende. Der Nennhub ergibt sich aus nutzbarem Weg plus zweimal Endreserve.\n\nGruß\nTechnik",
  },
  {
    id: "m-3012",
    folder: "inbox",
    date: "2026-10-08T10:00:00Z",
    from: "technik@beispiel-hydraulik-service.example",
    subject: "Technikfreigabe VG-2026-0923 Revision C",
    body: "Hallo,\n\nfür VG-2026-0923 Revision C empfehlen wir eine Endreserve ER-63 von 20 mm je Ende.\n\nGruß\nService-Technik",
  },
  {
    id: "m-3013",
    folder: "inbox",
    date: "2026-10-10T09:00:00Z",
    from: `technik@${L}`,
    subject: "Technikfreigabe VG-2026-0923 Revision C (neu)",
    body: "Hallo Vertrieb,\n\nneue Technikfreigabe zu VG-2026-0923 Revision C: Endreserve ER-63 beträgt 15 mm je Ende.\n\nGruß\nTechnik",
  },
  {
    id: "m-3015",
    folder: "inbox",
    date: "2026-10-02T09:00:00Z",
    from: `kalkulation@${L}`,
    subject: "Preisindizes VG-2026-0923",
    body: "Hallo Vertrieb,\n\nfür VG-2026-0923 gilt der Metallindex MI-ST mit 1,30 EUR/kg. Materialzuschlag je Stück = Zuschlagsmasse (kg) laut ERP × Indexwert.\n\nGruß\nKalkulation",
  },
  {
    id: "m-3020",
    folder: "inbox",
    date: "2026-10-07T07:00:00Z",
    from: `it@${L}`,
    subject: "Wartungsfenster ERP am Samstag",
    body: "Das ERP ist am Samstag von 6 bis 8 Uhr wegen Wartungsarbeiten nicht erreichbar.",
  },
]

const steel = (mass) => ({ surchargeMass: mass, metalIndex: "MI-ST" })
const yearly = (...scales) => [
  { from: "2026-01-01", to: "2026-12-31", unit: 1, scales: scales.map(([from, price]) => ({ from, price })) },
]

export const materials = [
  // KG-FL40
  { number: "HZ-40-200-FL-0", name: "Hydraulikzylinder 40/200 Flansch", group: "KG-FL40", bore: 40, stroke: 200, pressure: 160, mounting: "Flansch", certification: null, status: "aktiv", stock: 30, blockedStock: 0, stockWeeks: 2, productionWeeks: 7, ...steel(2.4), conditions: yearly([1, 380], [10, 360]) },
  { number: "HZ-40-200-FL-1", name: "Hydraulikzylinder 40/200 Flansch, 210 bar", group: "KG-FL40", bore: 40, stroke: 200, pressure: 210, mounting: "Flansch", certification: null, status: "aktiv", stock: 12, blockedStock: 0, stockWeeks: 2, productionWeeks: 7, ...steel(2.6), conditions: yearly([1, 410]) },
  { number: "HZ-40-200-FL-9", name: "Hydraulikzylinder 40/200 Flansch (Auslauf)", group: "KG-FL40", bore: 40, stroke: 200, pressure: 160, mounting: "Flansch", certification: null, status: "gesperrt", stock: 50, blockedStock: 0, stockWeeks: 1, productionWeeks: 9, ...steel(2.4), conditions: yearly([1, 330]) },
  // KG-GA63: every derivation of S-2 finds an active material, the right one is 330 mm
  { number: "HZ-63-300-GA-0", name: "Hydraulikzylinder 63/300 Gelenkauge", group: "KG-GA63", bore: 63, stroke: 300, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.2), conditions: yearly([1, 560]) },
  { number: "HZ-63-310-GA-0", name: "Hydraulikzylinder 63/310 Gelenkauge", group: "KG-GA63", bore: 63, stroke: 310, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.3), conditions: yearly([1, 570]) },
  { number: "HZ-63-320-GA-0", name: "Hydraulikzylinder 63/320 Gelenkauge", group: "KG-GA63", bore: 63, stroke: 320, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.4), conditions: yearly([1, 580]) },
  { number: "HZ-63-330-GA-0", name: "Hydraulikzylinder 63/330 Gelenkauge", group: "KG-GA63", bore: 63, stroke: 330, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.5), conditions: yearly([1, 590]) },
  { number: "HZ-63-334-GA-0", name: "Hydraulikzylinder 63/334 Gelenkauge", group: "KG-GA63", bore: 63, stroke: 334, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.5), conditions: yearly([1, 595]) },
  { number: "HZ-63-340-GA-0", name: "Hydraulikzylinder 63/340 Gelenkauge", group: "KG-GA63", bore: 63, stroke: 340, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.6), conditions: yearly([1, 600]) },
  { number: "HZ-63-324-GA-0", name: "Hydraulikzylinder 63/324 Gelenkauge", group: "KG-GA63", bore: 63, stroke: 324, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.4), conditions: yearly([1, 585]) },
  { number: "HZ-63-350-GA-0", name: "Hydraulikzylinder 63/350 Gelenkauge", group: "KG-GA63", bore: 63, stroke: 350, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.7), conditions: yearly([1, 610]) },
  { number: "HZ-63-330-GA-1", name: "Hydraulikzylinder 63/330 Gelenkauge, 160 bar", group: "KG-GA63", bore: 63, stroke: 330, pressure: 160, mounting: "Gelenkauge", certification: null, status: "aktiv", stock: 10, blockedStock: 0, stockWeeks: 3, productionWeeks: 8, ...steel(4.4), conditions: yearly([1, 540]) },
  // KG-FU80: after the amendment (280 bar) the only exact candidate is blocked
  { number: "HZ-80-400-FU-0", name: "Hydraulikzylinder 80/400 Fuß, 250 bar", group: "KG-FU80", bore: 80, stroke: 400, pressure: 250, mounting: "Fuß", certification: null, status: "aktiv", stock: 20, blockedStock: 0, stockWeeks: 2, productionWeeks: 9, ...steel(6.8), conditions: yearly([1, 890], [5, 860]) },
  { number: "HZ-80-400-FU-0H", name: "Hydraulikzylinder 80/400 Fuß, 280 bar", group: "KG-FU80", bore: 80, stroke: 400, pressure: 280, mounting: "Fuß", certification: null, status: "gesperrt", stock: 15, blockedStock: 0, stockWeeks: 2, productionWeeks: 11, ...steel(7.0), conditions: yearly([1, 940]) },
  { number: "HZ-80-400-FL-0H", name: "Hydraulikzylinder 80/400 Flansch, 280 bar", group: "KG-FU80", bore: 80, stroke: 400, pressure: 280, mounting: "Flansch", certification: null, status: "aktiv", stock: 15, blockedStock: 0, stockWeeks: 2, productionWeeks: 10, ...steel(7.1), conditions: yearly([1, 960]) },
  { number: "HZ-80-400-FU-DH", name: "Hydraulikzylinder 80/400 Fuß, 280 bar, DNV", group: "KG-FU80", bore: 80, stroke: 400, pressure: 280, mounting: "Fuß", certification: "DNV", status: "aktiv", stock: 15, blockedStock: 0, stockWeeks: 2, productionWeeks: 10, ...steel(7.0), conditions: yearly([1, 1020]) },
  { number: "HZ-80-410-FU-0H", name: "Hydraulikzylinder 80/410 Fuß, 280 bar", group: "KG-FU80", bore: 80, stroke: 410, pressure: 280, mounting: "Fuß", certification: null, status: "aktiv", stock: 15, blockedStock: 0, stockWeeks: 2, productionWeeks: 10, ...steel(7.1), conditions: yearly([1, 950]) },
]
