// The err404-layout world. All data is invented. It keeps the RFQ task and its 29 rules but splits the evidence
// differently from the public scenario: the bidder Q&A ("Clarifications") lives on the buyer's extranet next to the
// specification, every site is in English, and the bid form is a table of line items. DERIVATION.md explains how
// expected.json follows from this file; derive.py re-checks it.

export const scenarioDate = "2026-11-16T09:00:00Z"

export const bidder = {
  name: "Example Fluidtech GmbH",
  vatId: "DE999888777",
  mailbox: "sales@fluidtech.example",
}

export const tender = {
  reference: "TR-2026-0815",
  title: "Hydraulic cylinders for the quay gates, east basin",
  buyer: { name: "Sampletown Port Authority", street: "1 Harbour Road", postalCode: "99999", city: "Sampletown" },
  procedure: "Open procedure",
  publishedAt: "2026-10-12T08:00:00Z",
  deadline: "2026-12-04T11:00:00Z",
  currency: "EUR",
  positions: [
    { position: "10", text: "Lift cylinder, gate leaf A", quantity: 18, unit: "pcs", requirementRef: "R-10" },
    { position: "20", text: "Lift cylinder, gate leaf B", quantity: 12, unit: "pcs", requirementRef: "R-20" },
    { position: "30", text: "Clamping cylinder kit, sluice", quantity: 5, unit: "sets", requirementRef: "R-30" },
    { position: "40", text: "Tilt cylinder, flap gate", quantity: 8, unit: "pcs", requirementRef: "R-40" },
    { position: "50", text: "Ramp cylinder, ferry berth", quantity: 30, unit: "pcs", requirementRef: "R-50" },
    { position: "60", text: "Locking cylinder, mitre gate", quantity: 10, unit: "pcs", requirementRef: "R-60" },
  ],
}

/** Look-alike and unrelated notices on the sourcing hub. They take no electronic bid. */
export const otherTenders = [
  {
    reference: "TR-2026-0815-B",
    title: "Hydraulic cylinders for the quay gates, east basin: lot B (spare parts)",
    buyer: "Sampletown Port Authority",
    deadline: "2026-12-11T11:00:00Z",
    positions: [
      { position: "10", text: "Seal kit, lift cylinder", quantity: 40, unit: "pcs" },
      { position: "20", text: "Rod eye, spare", quantity: 16, unit: "pcs" },
    ],
  },
  {
    reference: "TR-2026-0851",
    title: "Pump spares for the dry dock",
    buyer: "Sampletown Port Authority",
    deadline: "2026-11-27T11:00:00Z",
    positions: [{ position: "10", text: "Gear pump, 40 cm3", quantity: 6, unit: "pcs" }],
  },
  {
    reference: "TR-2026-0790",
    title: "Lubricants for harbour cranes 2027",
    buyer: "Sampletown Port Authority",
    deadline: "2026-11-20T11:00:00Z",
    positions: [{ position: "10", text: "Hydraulic oil HLP 46, 208 l", quantity: 24, unit: "drums" }],
  },
]

// --- the buyer's extranet: the specification and the clarifications ---------------------------------------------

const req = (ref, bore, stroke, pressure, mounting, certification, group, extra = {}) => ({
  ref,
  bore,
  stroke,
  pressure,
  mounting,
  certification,
  group,
  ...extra,
})

export const specification = {
  number: "TS-4471",
  title: "Technical specification: hydraulic cylinders, quay gates east basin",
  revisions: [
    {
      revision: "A",
      date: "2026-09-28",
      status: "Superseded",
      note: "First issue",
      requirements: [
        req("R-10", 63, 400, 210, "Flange", null, "CG-63F"),
        req("R-20", 63, 400, 210, "Flange", null, "CG-63F"),
        req("R-30", 40, 150, 160, "Clevis", null, "CG-40C", { piecesPerSet: 5 }),
        req("R-40", 80, 500, 160, "Trunnion", "DNV", "CG-80T"),
        req("R-50", 50, 250, 160, "Rear eye", null, "CG-50E"),
        req("R-60", 100, null, 250, "Flange", "DNV", "CG-100F", { usableStroke: 370, reserveKey: "ER-60" }),
      ],
    },
    {
      revision: "B",
      date: "2026-10-14",
      status: "Superseded",
      note: "R-10, R-20: ATEX required; R-40: 210 bar; R-60: usable stroke 380 mm",
      requirements: [
        req("R-10", 63, 400, 210, "Flange", "ATEX", "CG-63F"),
        req("R-20", 63, 400, 210, "Flange", "ATEX", "CG-63F"),
        req("R-30", 40, 150, 160, "Clevis", null, "CG-40C", { piecesPerSet: 5 }),
        req("R-40", 80, 500, 210, "Trunnion", "DNV", "CG-80T"),
        req("R-50", 50, 250, 160, "Rear eye", null, "CG-50E"),
        req("R-60", 100, null, 250, "Flange", "DNV", "CG-100F", { usableStroke: 380, reserveKey: "ER-60" }),
      ],
    },
    {
      revision: "C",
      date: "2026-10-30",
      status: "Current",
      note: "R-30: kit of 4 cylinders; R-50: stroke 300 mm",
      requirements: [
        req("R-10", 63, 400, 210, "Flange", "ATEX", "CG-63F"),
        req("R-20", 63, 400, 210, "Flange", "ATEX", "CG-63F"),
        req("R-30", 40, 150, 160, "Clevis", null, "CG-40C", { piecesPerSet: 4 }),
        req("R-40", 80, 500, 210, "Trunnion", "DNV", "CG-80T"),
        req("R-50", 50, 300, 160, "Rear eye", null, "CG-50E"),
        req("R-60", 100, null, 250, "Flange", "DNV", "CG-100F", { usableStroke: 380, reserveKey: "ER-60" }),
      ],
    },
  ],
}

/** Bidder questions and the buyer's answers, published on the extranet. Dates are publication dates (UTC). */
export const clarifications = [
  {
    number: "Q-01",
    date: "2026-10-16T10:20:00Z",
    question: "Position 30 is listed in sets. How many cylinders make up one set?",
    answer: "Position 30 is tendered and delivered in sets. The composition of a set is defined in the governing revision of specification TS-4471.",
  },
  {
    number: "Q-02",
    date: "2026-10-19T08:45:00Z",
    question: "Do special commercial terms apply to individual positions?",
    answer: "Yes. Position 10 is assigned to commercial group (Konditionsgruppe) KG-B. Position 40 is assigned to commercial group (Konditionsgruppe) KG-N. All other positions have no commercial group.",
  },
  {
    number: "Q-03",
    date: "2026-10-21T13:05:00Z",
    question: "Which packaging tariff applies?",
    answer: "Packaging tariff keys: positions 10 and 20 use PK-S; positions 30 and 50 use PK-L; positions 40 and 60 use PK-H.",
  },
  {
    number: "Q-04",
    date: "2026-10-23T09:10:00Z",
    question: "Can the quantity of the clamping cylinder kits be confirmed?",
    answer: "The quantity of position 30 is increased to 6 sets.",
  },
  {
    number: "Q-05",
    date: "2026-10-27T15:30:00Z",
    question: "Revision B of TS-4471 shows a trunnion mounting for R-40. Is a foot mounting acceptable?",
    answer: "Technical amendment to TS-4471 Rev. B, requirement R-40: mounting changed to Foot. All other attributes unchanged.",
  },
  {
    number: "Q-06",
    date: "2026-11-02T11:00:00Z",
    question: "Are delivery commitments available for any position?",
    answer: "Position 20 uses delivery commitment key DC-2. Position 40 uses delivery commitment key DC-5.",
  },
  {
    number: "Q-07",
    date: "2026-11-03T14:15:00Z",
    question: "Will the ramp cylinder quantity change after the berth extension?",
    answer: "The quantity of position 50 is increased to 40 pcs.",
  },
  {
    number: "Q-08",
    date: "2026-11-04T10:00:00Z",
    question: "How are setup costs to be priced?",
    answer: "Ordinary setup fees, once per position: position 40 EUR 360.00, position 60 EUR 250.00. No other position has an ordinary setup fee. Shared setup pools: pool SP-NORTH has the members positions 10, 20 and 50; pool SP-SOUTH has the members positions 30, 40 and 60.",
  },
  {
    number: "Q-09",
    date: "2026-11-05T16:40:00Z",
    question: "Does the operating pressure of R-40 stay at 210 bar in the current revision?",
    answer: "Technical amendment to TS-4471 Rev. C, requirement R-40: operating pressure 250 bar instead of 210 bar. All other attributes of R-40 are unchanged.",
  },
  {
    number: "Q-10",
    date: "2026-11-09T09:30:00Z",
    question: "Is the increase for position 50 final?",
    answer: "Our answer Q-07 is withdrawn (zurückgezogen). The quantity of position 50 is as tendered.",
  },
  {
    number: "Q-11",
    date: "2026-11-10T12:00:00Z",
    question: "Is there a project bonus?",
    answer: "Positions 30, 50 and 60 take part in project bonus plan PB-7. The other positions do not.",
  },
  {
    number: "Q-12",
    date: "2026-11-12T08:15:00Z",
    question: "Can the submission deadline be extended?",
    answer: "No. The deadline stays as published on the sourcing hub.",
  },
]

export const extranetNotices = [
  {
    id: "supplier-code",
    title: "Supplier code of conduct",
    content: "All suppliers confirm the code of conduct once a year. Bids are submitted only on the sourcing hub.",
  },
  {
    id: "invoicing",
    title: "Invoicing address",
    content: "Invoices go to the accounts payable office of the port authority, quoting the purchase order number.",
  },
]

// --- the bidder's mailbox -----------------------------------------------------------------------------------------

const d = "@fluidtech.example"
/** The bidder's mail. folder: inbox or archive. The mail site lists each folder oldest first. */
export const messages = [
  {
    id: "m-201",
    folder: "archive",
    date: "2026-10-20T07:40:00Z",
    from: `leitung${d}`,
    subject: "Discount approval TR-2026-0815",
    body: `Hi sales team,

for tender TR-2026-0815 (quay gate cylinders, Sampletown) I approve a discount of 3 % on all positions.

Best regards
Head of Sales`,
  },
  {
    id: "m-202",
    folder: "inbox",
    date: "2026-11-05T15:10:00Z",
    from: `leitung${d}`,
    subject: "Updated discount approval TR-2026-0815",
    body: `Hi sales team,

this replaces my earlier approval for tender TR-2026-0815. I approve a discount of 4.5 %, provided the undiscounted net order value of the bid reaches at least EUR 56,400.00. Below that value no discount applies.

Best regards
Head of Sales`,
  },
  {
    id: "m-203",
    folder: "inbox",
    date: "2026-11-10T10:05:00Z",
    from: `leitung${d}`,
    subject: "Discount approval TR-2026-0815-B",
    body: `Hi sales team,

for the spare parts lot TR-2026-0815-B I approve a discount of 6 %.

Best regards
Head of Sales`,
  },
  {
    id: "m-204",
    folder: "inbox",
    date: "2026-11-20T08:00:00Z",
    from: `leitung${d}`,
    subject: "Discount approval TR-2026-0815 (planned)",
    body: `Hi sales team,

planned for the negotiation round: for TR-2026-0815 I will approve a discount of 7 % from this date.

Best regards
Head of Sales`,
  },
  {
    id: "m-205",
    folder: "inbox",
    date: "2026-11-12T13:30:00Z",
    from: `controlling${d}`,
    subject: "Discount proposal TR-2026-0815",
    body: `Hello,

controlling would support a discount of 5 % for TR-2026-0815. The decision is with the head of sales.

Controlling`,
  },
  {
    id: "m-211",
    folder: "archive",
    date: "2026-10-26T09:00:00Z",
    from: `finanzen${d}`,
    subject: "Devisenfixing TR-2026-0815",
    body: `FX fixing for tender TR-2026-0815:

FX key FX-USD-11: 1 EUR = 1.3100 USD

Finance`,
  },
  {
    id: "m-212",
    folder: "inbox",
    date: "2026-11-09T09:00:00Z",
    from: `finanzen${d}`,
    subject: "Devisenfixing TR-2026-0815 (update)",
    body: `Updated FX fixing for tender TR-2026-0815. It replaces the fixing of 26 Oct 2026.

FX key FX-USD-10: 1 EUR = 1.2650 USD
FX key FX-USD-11: 1 EUR = 1.2800 USD

Finance`,
  },
  {
    id: "m-213",
    folder: "inbox",
    date: "2026-11-11T09:00:00Z",
    from: `finanzen${d}`,
    subject: "Devisenfixing TR-2026-0851",
    body: `FX fixing for tender TR-2026-0851 (dry dock pumps):

FX key FX-USD-11: 1 EUR = 1.2500 USD

Finance`,
  },
  {
    id: "m-221",
    folder: "archive",
    date: "2026-10-02T07:00:00Z",
    from: `kalkulation${d}`,
    subject: "Preisindizes TR-2026-0815",
    body: `Metal price indices for tender TR-2026-0815 (EUR per tonne):

MX-ST1: 1,240.00 EUR/t
MX-ST2: 940.00 EUR/t

Costing`,
  },
  {
    id: "m-222",
    folder: "inbox",
    date: "2026-11-03T07:00:00Z",
    from: `kalkulation${d}`,
    subject: "Preisindizes TR-2026-0815 (Q4 windows)",
    body: `Metal price indices for tender TR-2026-0815, EUR per tonne. Each rate applies within its window, both dates included. This message replaces the index list of 2 Oct 2026.

MX-ST1
  2026-10-01 to 2026-10-31: 1,240.00 EUR/t
  2026-11-01 to 2026-11-30: 1,310.00 EUR/t
  2026-12-01 to 2026-12-31: 1,395.00 EUR/t
MX-ST2
  2026-10-01 to 2026-10-31: 940.00 EUR/t
  2026-11-01 to 2026-11-30: 980.00 EUR/t
  2026-12-01 to 2026-12-31: 1,020.00 EUR/t

The mass surcharge per piece is the material's surcharge weight in kg times the rate per kg.

Costing`,
  },
  {
    id: "m-231",
    folder: "archive",
    date: "2026-10-22T08:00:00Z",
    from: `kalkulation${d}`,
    subject: "Konditionsregeln TR-2026-0815",
    body: `Commercial groups for TR-2026-0815:

KG-B: the approved discount applies to the base price only.
KG-N: no discount applies to positions of this group.

Costing`,
  },
  {
    id: "m-232",
    folder: "inbox",
    date: "2026-11-06T08:30:00Z",
    from: `kalkulation${d}`,
    subject: "Konditionsregeln TR-2026-0815 (revised)",
    body: `Revised commercial groups for TR-2026-0815. They replace the rules of 22 Oct 2026.

KG-B: the approved discount applies to the base price and to the mass surcharge of the position.
KG-N: no discount applies to positions of this group; they are priced without any discount.

Setup fees and packaging are never discounted.

Costing`,
  },
  {
    id: "m-241",
    folder: "archive",
    date: "2026-10-28T10:00:00Z",
    from: `kalkulation${d}`,
    subject: "Rüstpools TR-2026-0815",
    body: `Shared setup pools for TR-2026-0815 (draft):

SP-NORTH: pool fee EUR 1,100.00
SP-SOUTH: pool fee EUR 700.00

Costing`,
  },
  {
    id: "m-242",
    folder: "inbox",
    date: "2026-11-06T11:15:00Z",
    from: `kalkulation${d}`,
    subject: "Rüstpools TR-2026-0815 (final)",
    body: `Final shared setup pools for TR-2026-0815. They replace the draft of 28 Oct 2026. Each pool fee is split over its members in proportion to final pieces times the material's setup allocation weight.

SP-NORTH: pool fee EUR 1,234.57. Round every member's share down to the cent, then hand out the remaining cents one at a time to the members with the largest remainders (ties: ascending position number).

SP-SOUTH: pool fee EUR 777.77. Round every member's share half up to the cent; the member with the largest weight (pieces times allocation weight) absorbs any difference to the pool fee, up or down.

Costing`,
  },
  {
    id: "m-251",
    folder: "inbox",
    date: "2026-11-04T09:45:00Z",
    from: `finanzen${d}`,
    subject: "Projektbonus TR-2026-0815",
    body: `Project bonus plan PB-7 for TR-2026-0815. Marginal bands on the eligible undiscounted goods total in EUR (final pieces times EUR base price); each rate applies only to the part of the total inside its band:

up to EUR 10,000.00: 0 %
above EUR 10,000.00 up to EUR 20,000.00: 1.5 %
above EUR 20,000.00: 2.5 %

Finance`,
  },
  {
    id: "m-252",
    folder: "inbox",
    date: "2026-11-08T09:45:00Z",
    from: `finanzen${d}`,
    subject: "Projektbonus TR-2026-0815-B",
    body: `Project bonus plan PB-7 for the spare parts lot TR-2026-0815-B: a flat 4 % on the goods total.

Finance`,
  },
  {
    id: "m-261",
    folder: "archive",
    date: "2026-10-21T14:00:00Z",
    from: `logistik${d}`,
    subject: "Verpackungstarife TR-2026-0815",
    body: `Packaging tariffs for TR-2026-0815, EUR per carton:

PK-S: 13.90
PK-L: 9.20
PK-H: 35.00

Logistics`,
  },
  {
    id: "m-262",
    folder: "inbox",
    date: "2026-11-02T14:00:00Z",
    from: `logistik${d}`,
    subject: "Verpackungstarife TR-2026-0815 (2027 rates)",
    body: `Packaging tariffs for TR-2026-0815, EUR per carton. They replace the tariffs of 21 Oct 2026.

PK-S: 14.50
PK-L: 9.80
PK-H: 38.00

Logistics`,
  },
  {
    id: "m-271",
    folder: "archive",
    date: "2026-10-29T08:20:00Z",
    from: `logistik${d}`,
    subject: "Terminfreigabe TR-2026-0815",
    body: `Delivery commitments for TR-2026-0815:

DC-2: material FT-6340-FA2, 5 weeks, while the pooled demand for the material is at most 30 pcs.

Logistics`,
  },
  {
    id: "m-272",
    folder: "inbox",
    date: "2026-11-08T08:20:00Z",
    from: `logistik${d}`,
    subject: "Terminfreigabe TR-2026-0815 (update)",
    body: `Delivery commitments for TR-2026-0815. They replace the commitments of 29 Oct 2026.

DC-2: material FT-6340-FA2, lead time 4 weeks, valid while the pooled final demand for this material is at most 30 pcs (inclusive).
DC-5: material FT-8050-TD2, lead time 3 weeks, valid while the pooled final demand for this material is at most 20 pcs (inclusive).

Logistics`,
  },
  {
    id: "m-281",
    folder: "archive",
    date: "2026-10-18T12:00:00Z",
    from: `technik${d}`,
    subject: "Technikfreigabe TR-2026-0815 Rev. B",
    body: `Engineering release for TR-2026-0815, specification TS-4471 Rev. B:

End reserve ER-60: 15 mm per end.

Engineering`,
  },
  {
    id: "m-282",
    folder: "inbox",
    date: "2026-11-07T12:00:00Z",
    from: `technik${d}`,
    subject: "Technikfreigabe TR-2026-0815 Rev. C",
    body: `Engineering release for TR-2026-0815, specification TS-4471 Rev. C:

End reserve ER-60: 10 mm per end. Nominal stroke = usable stroke + 2 x end reserve.

Engineering`,
  },
  {
    id: "m-291",
    folder: "inbox",
    date: "2026-11-01T06:00:00Z",
    from: `it${d}`,
    subject: "ERP maintenance on Saturday",
    body: "The operations suite is unavailable on Saturday from 06:00 to 08:00 UTC for maintenance.",
  },
  {
    id: "m-292",
    folder: "inbox",
    date: "2026-11-13T16:00:00Z",
    from: "newsletter@trade-fair.example",
    subject: "Fluid power fair: last call for exhibitors",
    body: "Book your booth for the fluid power fair before the end of the month.",
  },
]

// --- the operations suite (ERP) -----------------------------------------------------------------------------------

export const groups = {
  "CG-63F": "Flange cylinders, bore 63",
  "CG-40C": "Clevis cylinders, bore 40",
  "CG-80T": "Trunnion and foot cylinders, bore 80",
  "CG-50E": "Rear eye cylinders, bore 50",
  "CG-100F": "Heavy flange cylinders, bore 100",
  "CG-32R": "Compact cylinders, bore 32",
  "CG-125F": "Heavy flange cylinders, bore 125",
}

const cond = (from, to, currency, priceUnit, scales, fxKey = null) => ({ from, to, currency, priceUnit, scales, fxKey })
const scale = (min, price) => ({ min, price })

const mat = (number, group, bore, stroke, pressure, mounting, certification, status, more) => ({
  number,
  group,
  bore,
  stroke,
  pressure,
  mounting,
  certification,
  status,
  ...more,
})

/** leadStock / leadProduction: [value, unit], unit "weeks" or "working days" */
export const materials = [
  // CG-63F
  mat("FT-6340-FA2", "CG-63F", 63, 400, 210, "Flange", "ATEX", "Active", {
    carton: 4, surchargeKg: 18.5, allocationKg: 20, index: "MX-ST1",
    stock: 25, blocked: 20, leadStock: [2, "weeks"], leadProduction: [9, "weeks"],
    conditions: [
      cond("2026-01-01", "2026-06-30", "EUR", 1, [scale(1, 655), scale(15, 630), scale(30, 610)]),
      cond("2026-07-01", "2026-12-31", "EUR", 1, [scale(1, 642), scale(15, 618.4), scale(30, 596)]),
      cond("2027-01-01", "2027-06-30", "EUR", 1, [scale(1, 668), scale(15, 641.5), scale(30, 620)]),
    ],
  }),
  mat("FT-6340-FA1", "CG-63F", 63, 400, 210, "Flange", "ATEX", "Blocked", {
    carton: 4, surchargeKg: 19, allocationKg: 20, index: "MX-ST1",
    stock: 60, blocked: 0, leadStock: [1, "weeks"], leadProduction: [8, "weeks"],
    conditions: [cond("2026-01-01", "2026-12-31", "EUR", 1, [scale(1, 560), scale(15, 540)])],
  }),
  mat("FT-6340-F00", "CG-63F", 63, 400, 210, "Flange", null, "Active", {
    carton: 4, surchargeKg: 18.5, allocationKg: 20, index: "MX-ST1",
    stock: 80, blocked: 0, leadStock: [2, "weeks"], leadProduction: [6, "weeks"],
    conditions: [cond("2026-01-01", "2026-12-31", "EUR", 1, [scale(1, 590), scale(15, 571)])],
  }),
  mat("FT-6340-FA5", "CG-63F", 63, 400, 250, "Flange", "ATEX", "Active", {
    carton: 4, surchargeKg: 21, allocationKg: 22, index: "MX-ST1",
    stock: 40, blocked: 0, leadStock: [2, "weeks"], leadProduction: [10, "weeks"],
    conditions: [cond("2026-01-01", "2026-12-31", "EUR", 1, [scale(1, 702), scale(15, 684)])],
  }),
  mat("FT-6332-FA2", "CG-63F", 63, 320, 210, "Flange", "ATEX", "Active", {
    carton: 4, surchargeKg: 16, allocationKg: 18, index: "MX-ST1",
    stock: 35, blocked: 0, leadStock: [2, "weeks"], leadProduction: [8, "weeks"],
    conditions: [cond("2026-01-01", "2026-12-31", "EUR", 1, [scale(1, 611), scale(15, 590)])],
  }),
  // CG-40C
  mat("FT-4015-C00", "CG-40C", 40, 150, 160, "Clevis", null, "Active", {
    carton: 10, surchargeKg: 4.2, allocationKg: 5, index: "MX-ST2",
    stock: 30, blocked: 0, leadStock: [3, "weeks"], leadProduction: [6, "weeks"],
    conditions: [cond("2026-04-01", "2027-03-31", "EUR", 1, [scale(1, 118), scale(20, 109.5), scale(50, 101)])],
  }),
  mat("FT-4015-CA0", "CG-40C", 40, 150, 160, "Clevis", "ATEX", "Active", {
    carton: 10, surchargeKg: 4.4, allocationKg: 5, index: "MX-ST2",
    stock: 12, blocked: 0, leadStock: [3, "weeks"], leadProduction: [7, "weeks"],
    conditions: [cond("2026-04-01", "2027-03-31", "EUR", 1, [scale(1, 131), scale(20, 122)])],
  }),
  mat("FT-4016-C00", "CG-40C", 40, 160, 160, "Clevis", null, "Active", {
    carton: 10, surchargeKg: 4.4, allocationKg: 5, index: "MX-ST2",
    stock: 50, blocked: 0, leadStock: [2, "weeks"], leadProduction: [6, "weeks"],
    conditions: [cond("2026-04-01", "2027-03-31", "EUR", 1, [scale(1, 121), scale(20, 112)])],
  }),
  // CG-80T
  mat("FT-8050-TD5", "CG-80T", 80, 500, 250, "Trunnion", "DNV", "Active", {
    carton: 2, surchargeKg: 41, allocationKg: 45, index: "MX-ST1",
    stock: 5, blocked: 6, leadStock: [2, "weeks"], leadProduction: [40, "working days"],
    conditions: [
      cond("2026-09-01", "2026-11-15", "EUR", 1, [scale(1, 1460), scale(10, 1410)]),
      cond("2026-11-16", "2027-05-31", "EUR", 1, [scale(1, 1485), scale(10, 1430)]),
    ],
  }),
  mat("FT-8050-TD2", "CG-80T", 80, 500, 210, "Trunnion", "DNV", "Active", {
    carton: 2, surchargeKg: 39, allocationKg: 43, index: "MX-ST1",
    stock: 20, blocked: 0, leadStock: [2, "weeks"], leadProduction: [7, "weeks"],
    conditions: [cond("2026-07-01", "2027-06-30", "EUR", 1, [scale(1, 1395), scale(10, 1350)])],
  }),
  mat("FT-8050-FD5", "CG-80T", 80, 500, 250, "Foot", "DNV", "Active", {
    carton: 2, surchargeKg: 40, allocationKg: 44, index: "MX-ST1",
    stock: 15, blocked: 0, leadStock: [2, "weeks"], leadProduction: [9, "weeks"],
    conditions: [cond("2026-07-01", "2027-06-30", "EUR", 1, [scale(1, 1452), scale(10, 1401)])],
  }),
  // CG-50E
  mat("FT-5030-E00", "CG-50E", 50, 300, 160, "Rear eye", null, "Active", {
    carton: 6, surchargeKg: 9.6, allocationKg: 12, index: "MX-ST2",
    stock: 40, blocked: 0, leadStock: [1, "weeks"], leadProduction: [7, "weeks"],
    conditions: [
      cond("2026-01-01", "2026-09-30", "USD", 100, [scale(1, 30900), scale(25, 29100), scale(40, 27600)], "FX-USD-10"),
      cond("2026-10-01", "2027-03-31", "USD", 100, [scale(1, 31250), scale(25, 29443), scale(40, 27904)], "FX-USD-11"),
    ],
  }),
  mat("FT-5025-E00", "CG-50E", 50, 250, 160, "Rear eye", null, "Active", {
    carton: 6, surchargeKg: 8.8, allocationKg: 11, index: "MX-ST2",
    stock: 60, blocked: 0, leadStock: [1, "weeks"], leadProduction: [6, "weeks"],
    conditions: [cond("2026-10-01", "2027-03-31", "USD", 100, [scale(1, 29800), scale(25, 28150)], "FX-USD-11")],
  }),
  mat("FT-5030-E01", "CG-50E", 50, 300, 160, "Rear eye", null, "Blocked", {
    carton: 6, surchargeKg: 9.6, allocationKg: 12, index: "MX-ST2",
    stock: 90, blocked: 0, leadStock: [1, "weeks"], leadProduction: [5, "weeks"],
    conditions: [cond("2026-10-01", "2027-03-31", "USD", 100, [scale(1, 26000)], "FX-USD-11")],
  }),
  // CG-100F
  mat("FT-1040-FD5", "CG-100F", 100, 400, 250, "Flange", "DNV", "Active", {
    carton: 1, surchargeKg: 62, allocationKg: 70, index: "MX-ST1",
    stock: 12, blocked: 0, leadStock: [3, "weeks"], leadProduction: [10, "weeks"],
    conditions: [cond("2026-07-01", "2026-12-31", "USD", 1, [scale(1, 2140), scale(10, 2076.85)], "FX-USD-11")],
  }),
  mat("FT-1041-FD5", "CG-100F", 100, 410, 250, "Flange", "DNV", "Active", {
    carton: 1, surchargeKg: 63, allocationKg: 71, index: "MX-ST1",
    stock: 20, blocked: 0, leadStock: [3, "weeks"], leadProduction: [10, "weeks"],
    conditions: [cond("2026-07-01", "2026-12-31", "USD", 1, [scale(1, 2165), scale(10, 2101)], "FX-USD-11")],
  }),
  mat("FT-1038-FD5", "CG-100F", 100, 380, 250, "Flange", "DNV", "Active", {
    carton: 1, surchargeKg: 60, allocationKg: 68, index: "MX-ST1",
    stock: 20, blocked: 0, leadStock: [3, "weeks"], leadProduction: [10, "weeks"],
    conditions: [cond("2026-07-01", "2026-12-31", "USD", 1, [scale(1, 2110), scale(10, 2049)], "FX-USD-11")],
  }),
  mat("FT-1040-F05", "CG-100F", 100, 400, 250, "Flange", null, "Active", {
    carton: 1, surchargeKg: 62, allocationKg: 70, index: "MX-ST1",
    stock: 30, blocked: 0, leadStock: [2, "weeks"], leadProduction: [8, "weeks"],
    conditions: [cond("2026-07-01", "2026-12-31", "USD", 1, [scale(1, 1980), scale(10, 1922)], "FX-USD-11")],
  }),
]

// filler materials so that broad searches exceed the 20-result limit
for (const [group, bore, mounting] of [
  ["CG-32R", 32, "Rear eye"],
  ["CG-125F", 125, "Flange"],
]) {
  for (const stroke of [100, 160, 200, 250, 320, 400]) {
    materials.push(
      mat(`FT-${bore}${String(stroke).padStart(3, "0")}-X${bore === 32 ? "R" : "F"}0`, group, bore, stroke, 160, mounting, null, "Active", {
        carton: bore === 32 ? 12 : 1, surchargeKg: bore === 32 ? 1.8 : 95, allocationKg: bore === 32 ? 2 : 100,
        index: bore === 32 ? "MX-ST2" : "MX-ST1", stock: 24, blocked: 0, leadStock: [2, "weeks"],
        leadProduction: [8, "weeks"],
        conditions: [cond("2026-01-01", "2026-12-31", "EUR", 1, [scale(1, bore === 32 ? 64 + stroke / 10 : 2600 + stroke)])],
      }),
    )
  }
}
