// err404-traps: a bid scenario built around the rule traps the public scenario never triggers. All names, numbers
// and references are invented. The derivation of every expected value is in DERIVATION.md; change both together.
//
// Traps: a conditional discount whose inclusive threshold fails (an older unconditional approval must not revive),
// a withdrawn Q&A quantity change, a plain quantity change, approvals from wrong senders and domains, look-alike
// tender references, future-dated records (one on the scenario day after the scenario time), a delivery commitment
// whose pooled demand is one piece above its inclusive ceiling (and one exactly at it), stock pooled over two
// positions, price scales hit exactly at their minimum, a price condition and a metal-index window that start on the
// scenario date, and unit prices that end in exactly half a cent.

export const scenarioDate = "2026-10-08T09:00:00Z"

export const bidder = {
  name: "Beispiel Hydraulik GmbH",
  vatId: "DE999000123",
  mailbox: "vertrieb@beispiel-hydraulik.example",
}

export const tender = {
  reference: "VG-2026-0815",
  title: "Hydraulikzylinder für das Schleusentor Süd",
  buyer: { name: "Wasserverband Musterstadt", street: "Am Wehr 1", postalCode: "12345", city: "Musterstadt" },
  procedure: "Offenes Verfahren",
  publishedAt: "2026-09-21",
  deadline: "2026-10-30T11:00:00Z",
  currency: "EUR",
  positions: [
    { position: "10", text: "Hubzylinder Schütz Ost, Flanschbefestigung", quantity: 10, unit: "Stk", requirementRef: "R-10" },
    { position: "20", text: "Klappenzylinder Revisionsverschluss, Gelenkauge", quantity: 15, unit: "Stk", requirementRef: "R-20" },
    { position: "30", text: "Stellzylinder Tor Süd links, Fußbefestigung", quantity: 6, unit: "Stk", requirementRef: "R-30" },
    { position: "40", text: "Stellzylinder Tor Süd rechts, Fußbefestigung", quantity: 5, unit: "Stk", requirementRef: "R-40" },
  ],
}

/** Bidder Q&A on the tender portal ("Fragen und Antworten"), listed by date of receipt, not by number. */
export const questions = [
  {
    number: 1,
    date: "2026-09-29T08:00:00Z",
    question: "Sind die Preise netto anzugeben?",
    answer: "Ja, alle Preise sind netto in EUR anzugeben.",
  },
  {
    number: 2,
    date: "2026-10-01T10:15:00Z",
    question: "Bleibt es bei Position 10 bei der ausgeschriebenen Menge?",
    answer: "Nein. Die Menge von Position 10 erhöht sich auf 12 Stk.",
  },
  {
    number: 3,
    date: "2026-10-02T09:00:00Z",
    question: "Gibt es Änderungen an Position 20?",
    answer: "Ja. Für Position 20 werden 18 Stk benötigt.",
  },
  {
    number: 4,
    date: "2026-10-02T13:30:00Z",
    question: "Werden für einzelne Positionen verbindliche Liefertermine erwartet?",
    answer:
      "Ja. Für Position 10 gilt der Terminfreigabe-Schlüssel TF-10, für Position 40 der Schlüssel TF-40. Die Konditionen der Schlüssel teilt Ihnen Ihre Logistik mit.",
  },
  {
    number: 5,
    date: "2026-10-05T11:00:00Z",
    question: "Können Sie die Menge von Position 20 nochmals prüfen?",
    answer: "Für Position 20 werden nunmehr 25 Stk benötigt.",
  },
  {
    number: 6,
    date: "2026-10-06T15:45:00Z",
    question: "Gilt Antwort 5 weiterhin?",
    answer: "Antwort 5 wird zurückgezogen. Es gilt wieder der vorherige Stand.",
  },
  {
    number: 7,
    date: "2026-10-03T07:20:00Z",
    question: "Ändert sich an den Positionen 30 und 40 etwas?",
    answer: "Nein, die Mengen der Positionen 30 und 40 bleiben unverändert.",
  },
]

/** The buyer's technical specification (customer portal). Only the revision marked gültig counts. */
export const specification = {
  number: "TS-0815",
  title: "Technische Spezifikation Hydraulikzylinder Schleusentor Süd",
  revisions: [
    {
      revision: "A",
      date: "2026-09-10",
      status: "ersetzt",
      note: "Erstausgabe",
      requirements: [
        { ref: "R-10", group: "KG-FL50", bore: 50, stroke: 250, pressure: 160, mounting: "Flansch", certification: "ATEX" },
        { ref: "R-20", group: "KG-GA63", bore: 63, stroke: 400, pressure: 160, mounting: "Gelenkauge", certification: null },
        { ref: "R-30", group: "KG-FU80", bore: 80, stroke: 500, pressure: 250, mounting: "Fuß", certification: null },
        { ref: "R-40", group: "KG-FU80", bore: 80, stroke: 500, pressure: 250, mounting: "Fuß", certification: null },
      ],
    },
    {
      revision: "B",
      date: "2026-09-24",
      status: "gültig",
      note: "R-20: Druck 210 statt 160 bar; R-30 und R-40: DNV-Zertifizierung gefordert",
      requirements: [
        { ref: "R-10", group: "KG-FL50", bore: 50, stroke: 250, pressure: 160, mounting: "Flansch", certification: "ATEX" },
        { ref: "R-20", group: "KG-GA63", bore: 63, stroke: 400, pressure: 210, mounting: "Gelenkauge", certification: null },
        { ref: "R-30", group: "KG-FU80", bore: 80, stroke: 500, pressure: 250, mounting: "Fuß", certification: "DNV" },
        { ref: "R-40", group: "KG-FU80", bore: 80, stroke: 500, pressure: 250, mounting: "Fuß", certification: "DNV" },
      ],
    },
  ],
}

export const informationDocuments = [
  {
    id: "lieferantenleitfaden",
    number: "INF-001",
    title: "Leitfaden für Lieferanten",
    content: "Angebote geben Sie ausschließlich über das Vergabeportal ab. Klärungen erfolgen über die Bieterfragen.",
  },
]

export const otherTenders = [
  { reference: "VG-2026-0815-A", title: "Hydraulikzylinder Schleusentor Süd, Ersatzteile", buyer: "Wasserverband Musterstadt", deadline: "2026-11-12T11:00:00Z" },
  { reference: "VG-2026-0851", title: "Hydraulikaggregate Pumpwerk West", buyer: "Stadtwerke Musterstadt", deadline: "2026-11-05T11:00:00Z" },
]

const L = "beispiel-hydraulik.example"

/** The bidder's mailbox: folder "inbox" (Posteingang) or "archive" (Archiv). */
export const messages = [
  {
    id: "m-2001",
    folder: "inbox",
    date: "2026-09-22T08:30:00Z",
    from: "einkauf@wv-musterstadt.example",
    subject: "Ausschreibung VG-2026-0815 veröffentlicht",
    body: "Sehr geehrte Damen und Herren,\n\ndie Ausschreibung VG-2026-0815 ist im Vergabeportal veröffentlicht. Klärungen beantworten wir ausschließlich über die Bieterfragen im Portal.\n\nMit freundlichen Grüßen\nEinkauf, Wasserverband Musterstadt",
  },
  {
    id: "m-2002",
    folder: "archive",
    date: "2026-09-28T10:00:00Z",
    from: `leitung@${L}`,
    subject: "Rabattfreigabe VG-2026-0815",
    body: "Hallo Vertrieb,\n\nfür die Ausschreibung VG-2026-0815 genehmige ich einen Rabatt von 3 % auf alle Positionen.\n\nViele Grüße\nAnna Beispiel\nLeitung Vertrieb",
  },
  {
    id: "m-2003",
    folder: "inbox",
    date: "2026-10-06T16:20:00Z",
    from: `leitung@${L}`,
    subject: "Rabattfreigabe VG-2026-0815 (ersetzt Freigabe vom 28.09.)",
    body: "Hallo Vertrieb,\n\nmeine Freigabe vom 28.09. für VG-2026-0815 ersetze ich hiermit: Ich genehmige 4 % Rabatt, aber nur, wenn der Nettoauftragswert mindestens 27.300,00 EUR beträgt. Maßgeblich ist der Wert vor Rabatt aus den Staffelpreisen, ohne Materialzuschläge und ohne Rüstkosten. Wird der Wert nicht erreicht, gibt es keinen Rabatt.\n\nViele Grüße\nAnna Beispiel\nLeitung Vertrieb",
  },
  {
    id: "m-2004",
    folder: "inbox",
    date: "2026-10-07T09:10:00Z",
    from: `leitung@${L}`,
    subject: "Rabattfreigabe VG-2026-0815-A",
    body: "Hallo Vertrieb,\n\nfür VG-2026-0815-A (Ersatzteile) genehmige ich 6 % Rabatt ohne Bedingungen.\n\nViele Grüße\nAnna Beispiel\nLeitung Vertrieb",
  },
  {
    id: "m-2005",
    folder: "inbox",
    date: "2026-10-07T11:40:00Z",
    from: "leitung@beispiel-hydraulik-gruppe.example",
    subject: "Rabattfreigabe VG-2026-0815",
    body: "Hallo zusammen,\n\naus Sicht der Gruppe ist für VG-2026-0815 ein Rabatt von 7 % vertretbar.\n\nGruß\nKonzernvertrieb",
  },
  {
    id: "m-2006",
    folder: "inbox",
    date: "2026-10-07T14:05:00Z",
    from: `assistenz@${L}`,
    subject: "WG: Rabattfreigabe VG-2026-0815",
    body: "Hallo Vertrieb,\n\nFrau Beispiel lässt ausrichten, dass für VG-2026-0815 auch 5 % ohne Mindestwert gehen.\n\nGruß\nAssistenz der Geschäftsleitung",
  },
  {
    id: "m-2007",
    folder: "inbox",
    date: "2026-10-09T08:00:00Z",
    from: `leitung@${L}`,
    subject: "Rabattfreigabe VG-2026-0815 Nachtrag",
    body: "Hallo Vertrieb,\n\nNachtrag zu VG-2026-0815: Ab sofort genehmige ich 5 % Rabatt ohne Mindestauftragswert.\n\nViele Grüße\nAnna Beispiel\nLeitung Vertrieb",
  },
  {
    id: "m-2010",
    folder: "archive",
    date: "2026-09-30T07:45:00Z",
    from: `logistik@${L}`,
    subject: "Terminfreigabe VG-2026-0815",
    body: "Hallo Vertrieb,\n\nTerminfreigaben für VG-2026-0815:\n- TF-10: Material HZ-50-250-FL-X, bis einschließlich 15 Stück gepoolter Bedarf je Material, Liefertermin 5 Wochen.\n- TF-40: Material HZ-80-500-FU-D, bis einschließlich 12 Stück gepoolter Bedarf je Material, Liefertermin 4 Wochen.\n\nGruß\nLogistik",
  },
  {
    id: "m-2011",
    folder: "inbox",
    date: "2026-10-05T12:00:00Z",
    from: `logistik@${L}`,
    subject: "Terminfreigabe VG-2026-0815 (aktualisiert)",
    body: "Hallo Vertrieb,\n\ndiese Terminfreigabe ersetzt die vom 30.09. für VG-2026-0815:\n- TF-10: Material HZ-50-250-FL-X, bis einschließlich 12 Stück gepoolter Bedarf je Material, Liefertermin 4 Wochen.\n- TF-40: Material HZ-80-500-FU-D, bis einschließlich 10 Stück gepoolter Bedarf je Material, Liefertermin 3 Wochen.\nLiegt der gepoolte Bedarf über der Grenze, gilt die normale Lieferzeit.\n\nGruß\nLogistik",
  },
  {
    id: "m-2012",
    folder: "inbox",
    date: "2026-10-06T09:30:00Z",
    from: "logistik@beispiel-hydraulik-service.example",
    subject: "Terminfreigabe VG-2026-0815",
    body: "Hallo,\n\nfür VG-2026-0815 können wir TF-40 für HZ-80-500-FU-D bis 20 Stück mit 2 Wochen zusagen.\n\nGruß\nService-Logistik",
  },
  {
    id: "m-2013",
    folder: "inbox",
    date: "2026-10-10T07:00:00Z",
    from: `logistik@${L}`,
    subject: "Terminfreigabe VG-2026-0815 Erweiterung",
    body: "Hallo Vertrieb,\n\nfür VG-2026-0815 erweitern wir TF-40 (HZ-80-500-FU-D) auf bis einschließlich 20 Stück gepoolten Bedarf, Liefertermin 3 Wochen.\n\nGruß\nLogistik",
  },
  {
    id: "m-2020",
    folder: "archive",
    date: "2026-08-20T10:00:00Z",
    from: `kalkulation@${L}`,
    subject: "Preisindizes VG-2026-0815",
    body: "Hallo Vertrieb,\n\nfür VG-2026-0815 gilt der Metallindex MI-ST mit 1,10 EUR/kg.\n\nGruß\nKalkulation",
  },
  {
    id: "m-2021",
    folder: "inbox",
    date: "2026-10-01T09:00:00Z",
    from: `kalkulation@${L}`,
    subject: "Preisindizes VG-2026-0815",
    body: "Hallo Vertrieb,\n\nfür VG-2026-0815 gilt der Metallindex MI-ST mit folgenden Gültigkeitsfenstern (jeweils einschließlich):\n- 01.09.2026 bis 07.10.2026: 1,20 EUR/kg\n- 08.10.2026 bis 31.10.2026: 1,35 EUR/kg\n- ab 01.11.2026: 1,50 EUR/kg\nMaterialzuschlag je Stück = Zuschlagsmasse (kg) laut ERP × Indexwert.\n\nGruß\nKalkulation",
  },
  {
    id: "m-2022",
    folder: "inbox",
    date: "2026-10-08T14:00:00Z",
    from: `kalkulation@${L}`,
    subject: "Preisindizes VG-2026-0815 Korrektur",
    body: "Hallo Vertrieb,\n\nKorrektur für VG-2026-0815: MI-ST vom 08.10.2026 bis 31.10.2026 beträgt 1,42 EUR/kg.\n\nGruß\nKalkulation",
  },
  {
    id: "m-2023",
    folder: "inbox",
    date: "2026-10-04T10:00:00Z",
    from: `kalkulation@${L}`,
    subject: "Preisindizes VG-2026-0851",
    body: "Hallo Vertrieb,\n\nfür VG-2026-0851 gilt MI-ST ab 01.10.2026 mit 1,60 EUR/kg.\n\nGruß\nKalkulation",
  },
  {
    id: "m-2030",
    folder: "inbox",
    date: "2026-10-07T07:00:00Z",
    from: `it@${L}`,
    subject: "Wartungsfenster ERP am Samstag",
    body: "Das ERP ist am Samstag von 6 bis 8 Uhr wegen Wartungsarbeiten nicht erreichbar.",
  },
]

/**
 * ERP materials. `conditions`: price condition records (EUR, price per `unit` pieces, scales by minimum quantity),
 * each valid from/to inclusive. surchargeMass: kg per piece for the metal surcharge (index `metalIndex`).
 */
export const materials = [
  // KG-FL50
  {
    number: "HZ-50-250-FL-X", name: "Hydraulikzylinder 50/250 Flansch, ATEX", group: "KG-FL50",
    bore: 50, stroke: 250, pressure: 160, mounting: "Flansch", certification: "ATEX", status: "aktiv",
    stock: 5, blockedStock: 20, stockWeeks: 2, productionWeeks: 10, surchargeMass: 3.3, metalIndex: "MI-ST",
    conditions: [
      { from: "2026-01-01", to: "2026-10-07", unit: 1, scales: [{ from: 1, price: 520 }, { from: 12, price: 495 }] },
      { from: "2026-10-08", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 500 }, { from: 12, price: 470 }, { from: 25, price: 450 }] },
    ],
  },
  {
    number: "HZ-50-250-FL-XB", name: "Hydraulikzylinder 50/250 Flansch, ATEX (Altserie)", group: "KG-FL50",
    bore: 50, stroke: 250, pressure: 160, mounting: "Flansch", certification: "ATEX", status: "gesperrt",
    stock: 40, blockedStock: 0, stockWeeks: 1, productionWeeks: 12, surchargeMass: 3.3, metalIndex: "MI-ST",
    conditions: [{ from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 430 }] }],
  },
  {
    number: "HZ-50-250-FL-0", name: "Hydraulikzylinder 50/250 Flansch", group: "KG-FL50",
    bore: 50, stroke: 250, pressure: 160, mounting: "Flansch", certification: null, status: "aktiv",
    stock: 30, blockedStock: 0, stockWeeks: 2, productionWeeks: 6, surchargeMass: 3.1, metalIndex: "MI-ST",
    conditions: [{ from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 410 }] }],
  },
  {
    number: "HZ-50-250-FL-X2", name: "Hydraulikzylinder 50/250 Flansch, ATEX, 210 bar", group: "KG-FL50",
    bore: 50, stroke: 250, pressure: 210, mounting: "Flansch", certification: "ATEX", status: "aktiv",
    stock: 15, blockedStock: 0, stockWeeks: 2, productionWeeks: 9, surchargeMass: 3.6, metalIndex: "MI-ST",
    conditions: [{ from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 560 }] }],
  },
  // KG-GA63
  {
    number: "HZ-63-400-GA-0", name: "Hydraulikzylinder 63/400 Gelenkauge, 210 bar", group: "KG-GA63",
    bore: 63, stroke: 400, pressure: 210, mounting: "Gelenkauge", certification: null, status: "aktiv",
    stock: 20, blockedStock: 6, stockWeeks: 3, productionWeeks: 8, surchargeMass: 5.1, metalIndex: "MI-ST",
    conditions: [
      { from: "2026-07-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 640 }, { from: 10, price: 615 }, { from: 20, price: 590 }] },
    ],
  },
  {
    number: "HZ-63-400-GA-1", name: "Hydraulikzylinder 63/400 Gelenkauge, 160 bar", group: "KG-GA63",
    bore: 63, stroke: 400, pressure: 160, mounting: "Gelenkauge", certification: null, status: "aktiv",
    stock: 50, blockedStock: 0, stockWeeks: 2, productionWeeks: 6, surchargeMass: 4.8, metalIndex: "MI-ST",
    conditions: [{ from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 575 }] }],
  },
  {
    number: "HZ-63-400-GA-9", name: "Hydraulikzylinder 63/400 Gelenkauge, 210 bar (Auslauf)", group: "KG-GA63",
    bore: 63, stroke: 400, pressure: 210, mounting: "Gelenkauge", certification: null, status: "gesperrt",
    stock: 30, blockedStock: 0, stockWeeks: 1, productionWeeks: 12, surchargeMass: 5.1, metalIndex: "MI-ST",
    conditions: [{ from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 520 }] }],
  },
  // KG-FU80
  {
    number: "HZ-80-500-FU-D", name: "Hydraulikzylinder 80/500 Fuß, DNV", group: "KG-FU80",
    bore: 80, stroke: 500, pressure: 250, mounting: "Fuß", certification: "DNV", status: "aktiv",
    stock: 10, blockedStock: 4, stockWeeks: 2, productionWeeks: 9, surchargeMass: 7.7, metalIndex: "MI-ST",
    conditions: [
      { from: "2025-01-01", to: "2025-12-31", unit: 1, scales: [{ from: 1, price: 950 }, { from: 6, price: 920 }] },
      { from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 980 }, { from: 6, price: 940 }, { from: 10, price: 910 }] },
    ],
  },
  {
    number: "HZ-80-500-FU-D9", name: "Hydraulikzylinder 80/500 Fuß, DNV (gesperrt)", group: "KG-FU80",
    bore: 80, stroke: 500, pressure: 250, mounting: "Fuß", certification: "DNV", status: "gesperrt",
    stock: 25, blockedStock: 0, stockWeeks: 1, productionWeeks: 9, surchargeMass: 7.7, metalIndex: "MI-ST",
    conditions: [{ from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 870 }] }],
  },
  {
    number: "HZ-80-500-FL-D", name: "Hydraulikzylinder 80/500 Flansch, DNV", group: "KG-FU80",
    bore: 80, stroke: 500, pressure: 250, mounting: "Flansch", certification: "DNV", status: "aktiv",
    stock: 30, blockedStock: 0, stockWeeks: 2, productionWeeks: 9, surchargeMass: 7.9, metalIndex: "MI-ST",
    conditions: [{ from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 1010 }] }],
  },
  {
    number: "HZ-80-500-FU-0", name: "Hydraulikzylinder 80/500 Fuß", group: "KG-FU80",
    bore: 80, stroke: 500, pressure: 250, mounting: "Fuß", certification: null, status: "aktiv",
    stock: 30, blockedStock: 0, stockWeeks: 2, productionWeeks: 7, surchargeMass: 7.5, metalIndex: "MI-ST",
    conditions: [{ from: "2026-01-01", to: "2026-12-31", unit: 1, scales: [{ from: 1, price: 890 }] }],
  },
]
