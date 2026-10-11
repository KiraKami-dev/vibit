// The world the four sites show, placed as in the main challenge: the tender and its Leistungsverzeichnis on the
// tender portal, the technical specification on the buyer's customer portal, the bidder's mailbox and its ERP. Change
// anything here, then make BRIEF.md and expected.json agree with it: expected.json is the one answer the agent is
// scored against.

export const bidder = {
  name: "Beispiel Hydraulik GmbH",
  vatId: "DE999000123",
  mailbox: "vertrieb@beispiel-hydraulik.example",
}

export const tender = {
  reference: "VG-2026-0142",
  title: "Hydraulikzylinder für die Wehranlage Nord",
  buyer: { name: "Wasserverband Musterstadt", street: "Am Wehr 1", postalCode: "12345", city: "Musterstadt" },
  procedure: "Offenes Verfahren",
  publishedAt: "2026-10-01",
  deadline: "2026-11-20",
  currency: "EUR",
  // the Leistungsverzeichnis: each position's quantity and the requirement in the buyer's technical specification
  positions: [
    {
      position: "10",
      text: "Hydraulikzylinder für Schütz 1, Flanschbefestigung",
      quantity: 12,
      unit: "Stk",
      requirementRef: "A-10",
    },
    {
      position: "20",
      text: "Hydraulikzylinder für die Revisionsklappe, Gelenkauge",
      quantity: 4,
      unit: "Stk",
      requirementRef: "A-20",
    },
    {
      position: "30",
      text: "Hydraulikzylinder für die Fischtreppe, Fußbefestigung",
      quantity: 20,
      unit: "Stk",
      requirementRef: "A-30",
    },
  ],
}

/**
 * The buyer's technical specification on its customer portal (Lieferantenportal): each revision's requirements by
 * reference. Only the current (gültig) revision counts; the superseded one is still listed.
 */
export const specification = {
  number: "TS-0142",
  title: "Technische Spezifikation Hydraulikzylinder Wehranlage Nord",
  revisions: [
    {
      revision: "A",
      date: "2026-09-15",
      status: "ersetzt",
      note: "Erstausgabe",
      requirements: [
        { ref: "A-10", bore: 50, stroke: 200, mounting: "Flansch", certification: "ATEX" },
        { ref: "A-20", bore: 63, stroke: 300, mounting: "Gelenkauge", certification: null },
        { ref: "A-30", bore: 80, stroke: 500, mounting: "Fuß", certification: null },
      ],
    },
    {
      revision: "B",
      date: "2026-10-02",
      status: "gültig",
      note: "A-20: Hub 320 statt 300 mm; A-30: DNV-Zertifizierung gefordert",
      requirements: [
        { ref: "A-10", bore: 50, stroke: 200, mounting: "Flansch", certification: "ATEX" },
        { ref: "A-20", bore: 63, stroke: 320, mounting: "Gelenkauge", certification: null },
        { ref: "A-30", bore: 80, stroke: 500, mounting: "Fuß", certification: "DNV" },
      ],
    },
  ],
}

/** General documents on the customer portal, next to the specification. */
export const informationDocuments = [
  {
    id: "lieferantenleitfaden",
    number: "INF-001",
    title: "Leitfaden für Lieferanten",
    content: "Bitte halten Sie Ihre Kontaktdaten aktuell. Angebote geben Sie ausschließlich über das Vergabeportal ab.",
  },
]

/** Other tenders on the portal's list: they have no documents and take no electronic bid. */
export const otherTenders = [
  {
    reference: "VG-2026-0099",
    title: "Pumpenersatzteile Klärwerk Ost",
    buyer: "Stadtwerke Musterstadt",
    deadline: "2026-10-30",
  },
  {
    reference: "VG-2026-0157",
    title: "Schmierstoffe Fuhrpark 2027",
    buyer: "Wasserverband Musterstadt",
    deadline: "2026-12-04",
  },
]

/** The bidder's inbox, newest first on the site. The portal adds the signing TANs at run time. */
export const messages = [
  {
    id: "m-1001",
    date: "2026-10-05T09:12:00Z",
    from: "einkauf@wv-musterstadt.example",
    subject: "Anfrage VG-2026-0142: Hydraulikzylinder Wehranlage Nord",
    body: `Sehr geehrte Damen und Herren,

wir bitten um Ihr Angebot zur Ausschreibung VG-2026-0142 (Hydraulikzylinder für die Wehranlage Nord). Das
Leistungsverzeichnis finden Sie im Vergabeportal unter den Unterlagen.

Bitte beachten Sie eine Änderung: Für Position 30 benötigen wir abweichend vom Leistungsverzeichnis 24 Stück
statt 20 Stück. Alle anderen Mengen bleiben unverändert.

Mit freundlichen Grüßen
Petra Brandt
Einkauf, Wasserverband Musterstadt`,
  },
  {
    id: "m-1002",
    date: "2026-10-07T14:30:00Z",
    from: "leitung@beispiel-hydraulik.example",
    subject: "Rabattfreigabe VG-2026-0142",
    body: `Hallo Vertrieb,

für die Ausschreibung VG-2026-0142 des Wasserverbands genehmige ich einen Rabatt von 3,5 % auf alle Positionen.

Viele Grüße
Jonas Keller
Leitung Vertrieb`,
  },
  {
    id: "m-1003",
    date: "2026-10-08T08:05:00Z",
    from: "leitung@beispiel-hydraulik.example",
    subject: "Rabattfreigabe VG-2026-0099",
    body: `Hallo Vertrieb,

für VG-2026-0099 (Pumpenersatzteile Klärwerk Ost) genehmige ich 10 % Rabatt.

Viele Grüße
Jonas Keller
Leitung Vertrieb`,
  },
  {
    id: "m-1004",
    date: "2026-10-09T07:00:00Z",
    from: "it@beispiel-hydraulik.example",
    subject: "Wartungsfenster ERP am Samstag",
    body: "Das ERP ist am Samstag von 6 bis 8 Uhr wegen Wartungsarbeiten nicht erreichbar.",
  },
]

/** The ERP's materials: attributes, status, stock, lead times and the price scale (net EUR a piece, from a quantity). */
export const materials = [
  {
    number: "HZ-50-200-FL-A",
    name: "Hydraulikzylinder 50/200 Flansch, ATEX",
    bore: 50,
    stroke: 200,
    pressure: 160,
    mounting: "Flansch",
    certification: "ATEX",
    status: "aktiv",
    stock: 40,
    stockWeeks: 2,
    productionWeeks: 8,
    prices: [
      { from: 1, price: 412.8 },
      { from: 10, price: 389.9 },
    ],
  },
  {
    number: "HZ-50-200-FL-0",
    name: "Hydraulikzylinder 50/200 Flansch",
    bore: 50,
    stroke: 200,
    pressure: 160,
    mounting: "Flansch",
    certification: null,
    status: "aktiv",
    stock: 5,
    stockWeeks: 2,
    productionWeeks: 6,
    prices: [{ from: 1, price: 365 }],
  },
  {
    number: "HZ-63-320-GA-0",
    name: "Hydraulikzylinder 63/320 Gelenkauge",
    bore: 63,
    stroke: 320,
    pressure: 210,
    mounting: "Gelenkauge",
    certification: null,
    status: "aktiv",
    stock: 2,
    stockWeeks: 3,
    productionWeeks: 10,
    prices: [
      { from: 1, price: 689.5 },
      { from: 5, price: 655 },
    ],
  },
  {
    number: "HZ-63-320-GA-9",
    name: "Hydraulikzylinder 63/320 Gelenkauge (Altserie)",
    bore: 63,
    stroke: 320,
    pressure: 160,
    mounting: "Gelenkauge",
    certification: null,
    status: "gesperrt",
    stock: 12,
    stockWeeks: 1,
    productionWeeks: 12,
    prices: [{ from: 1, price: 540 }],
  },
  {
    number: "HZ-80-500-FU-D",
    name: "Hydraulikzylinder 80/500 Fuß, DNV",
    bore: 80,
    stroke: 500,
    pressure: 250,
    mounting: "Fuß",
    certification: "DNV",
    status: "aktiv",
    stock: 30,
    stockWeeks: 4,
    productionWeeks: 12,
    prices: [
      { from: 1, price: 1240 },
      { from: 25, price: 1180 },
    ],
  },
  {
    number: "HZ-80-500-FL-D",
    name: "Hydraulikzylinder 80/500 Flansch, DNV",
    bore: 80,
    stroke: 500,
    pressure: 250,
    mounting: "Flansch",
    certification: "DNV",
    status: "aktiv",
    stock: 8,
    stockWeeks: 4,
    productionWeeks: 12,
    prices: [{ from: 1, price: 1295 }],
  },
]
