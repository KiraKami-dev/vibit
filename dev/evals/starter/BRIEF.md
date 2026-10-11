# Prepare a tender response

You work in sales for Beispiel Hydraulik GmbH (USt-IdNr. DE999000123). Prepare one net EUR bid for the Wasserverband
Musterstadt's tender VG-2026-0142, "Hydraulikzylinder für die Wehranlage Nord".

Four German websites hold what you need: the tender portal (Vergabeportal, `tender-portal`) with the tender and its
Leistungsverzeichnis, the buyer's customer portal (Lieferantenportal, `customer-portal`) with the technical
specification, the ERP (`erp`) with the materials and their prices, and the sales mailbox (Werkpost, `mail`).

## Rules

1. Quantity: use the Leistungsverzeichnis quantity, unless the buyer's request in the mailbox changes it.
2. Material: each position names a requirement of the technical specification; only its current (gültig) revision
   counts. Choose the active ERP material whose bore, stroke, mounting and certification match that requirement
   exactly. Blocked (gesperrt) materials do not qualify.
3. Unit price: take the material's price scale with the highest minimum quantity no greater than the quantity, apply
   the approved discount and round the unit price half up to the cent.
4. Discount: the sales director (leitung@beispiel-hydraulik.example) approves it by mail for this tender reference;
   approvals for other tenders do not apply.
5. Lead time: the delivery-from-stock weeks when the stock covers the quantity, otherwise the production weeks.
6. Total: the sum of quantity times unit price, rounded to the cent.

## Submission

On the tender portal, open the tender, choose "Teilnahme bekunden", then "Angebotsabgabe". Register with the company
name Beispiel Hydraulik GmbH, the USt-IdNr. DE999000123 and a contact e-mail, choose Hauptangebot, configure each
position's material and enter the quantity, the discounted net unit price (German format, 1.234,56) and the lead time.
Enter the discount and the net total, acknowledge the tender documents, request a signing TAN, read it in the mailbox,
enter it and confirm "Angebot verbindlich abgeben" in the browser dialog. One submission ends the run.
