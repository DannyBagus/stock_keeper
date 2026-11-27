# SumUp → KLARA: Reconciliation re-engineeren (Analyse 2026-11-27, freigegeben, Schritte 0–4 + 7–8 umgesetzt)

## Befund (Ist-Daten, read-only geprüft)
- KLARA Bankabgleich: SumUp-Gutschriften **05.08. (3352.00), 03.09. (3685.42), 05.10. (3150.24), 04.11. (1890.55) unverbucht**.
- SK-Abgleiche: Aug Delta +63.53, Sep −9.00, Okt/Nov nicht gestartet. Jan–Jul gebucht, teils mit Monaten Verzug.
- Exakter Test (SumUp-Payout-Zeile ↔ `Sale.transaction_id`) Jul–Nov: Identität
  `Bank = Σ Brutto − Σ Gebühr − Σ Refund-Abzüge` geht **jeden Monat auf den Rappen auf**.
  SK-Abdeckung via tx-Code seit April ~97–100 %.

## Ursachen
1. REFUND-Transaktionen tragen denselben `transaction_code` wie die Zahlung → alter Client behandelt sie als Zahlung.
   Aug: Refund 64.95 auf #628 wurde als Phantom-Sale #753 (DIVERSES, +64.95) nachgebucht **und** als Refund-Abzug 65.94 → Delta 63.53 (= 64.95 − 1.42 doppelt gezählte Gebühr).
2. Heuristik-Matching (Betrag+Zeit, Periode aus Timestamps ±5 Tage) statt exakt über tx-Code → Verwechslungen:
   Sale #926 ist Duplikat von #708 (TAAA4Z9T36D); TAAA4Z9QT2K (5.00) fehlt dafür.
3. Auflösung „Manuell/Ignoriert" hat keine Buchungswirkung → Delta bleibt (Sep: 9.00 TAAA4VPSS4M).
4. Klärung zu spät (Monat später, durch Buchhaltung statt Kasse). Vergessene Café-Kleinbeträge werden als DIVERSES = „Laden" nacherfasst → Laden/Café-Split falsch (Jul Café = 0).
5. Abtippen nach KLARA; Beleg sagt 6800, KLARA nutzt 6000 → Medienbruch.
6. Buchungsdatum = Auszahlungsdatum → Dezember-Umsatz landet im Folgejahr.
7. Latent: Pagination `sumup_client.get_transactions_in_window` bricht bei >100 Txn still ab (href ist nur Query-String → Exception → `break`).
8. Sales werden hart gelöscht (Aug: 298.75 TAAA32NPUZL, ReconciliationItem.sale jetzt NULL).

## Zielbild
Buchhalterisch über Durchlaufkonto **1101 Forderungen gegenüber PSP** (existiert in KLARA):
- **A. Monatsabschluss (Buchungsdatum = Periodenende/Payout-Periode):** automatisch aus SumUp-Daten
  - Soll 1101 / Haben 3000 Laden (Tag „Erlös SupportElle")
  - Soll 1101 / Haben 3000 Café (Tag „Erlös Gastro")
  - Soll 3800 / Haben 1101 Rückerstattungen
  - Soll 6000 / Haben 1101 SumUp-Gebühren (inkl. nicht erstatteter Refund-Gebühr)
  - Beleg: generiertes PDF (Payout-Liste je Txn + Split) angehängt
- **B. Bankgutschrift:** Soll 1020 / Haben 1101, Betrag = Payout-Netto. Im KLARA-Bankabgleich 1 Zeile, eindeutig über Referenz `SUMUP PIDxxxx PAYOUT ddmmyy` (= SumUp `payout.reference`).
- Kontrolle: Saldo 1101 nach B = 0 → Beweis „keine Differenz". Kein Linking-Problem der API mehr nötig.

Technisch (Stock Keeper):
- Payout-Erkennung automatisch (Cron täglich), kein Eintippen von Betrag/Datum mehr.
- Matching nur noch exakt: Payout-Zeile.transaction_code ↔ Sale.transaction_id; PAYMENT/REFUND sauber getrennt.
- Erlöszahlen kommen aus SumUp (Ground Truth fürs Geld); SK liefert nur Laden/Café-Split.
- Ausnahmen täglich an die Kasse (bestehendes `check_unmatched_sumup`), nicht monatlich an Buchhaltung.
- Monatlicher Abgleich nur noch Freigabe: „grün" → KLARA-Buchung vorbereiten → Freigabe-Klick (2FA) durch Alessia/Daniel.

## Umsetzung
- [x] 0. Altlasten: v2-Abgleiche #19–#22 (Aug–Nov) angelegt, alte #11/#12 → SUPERSEDED. #753 (Phantom aus Refund) + #926 (Duplikat von #708) storniert. 298.75 = gelöschter Sale #579 (Laden, Ware schon ausgebucht) → nur Kanal Laden gesetzt.
- [ ] 0b. Offen für Laden/Alessia: Sep 9.00 (20.08. 13:54) + 5.00 (27.08. 13:35) fehlen in SK; Okt #767 9.00 + #768 5.00 (17.09., ohne SumUp-Code) + 8.50 fehlt (24.09.); Nov #820 99.00 ohne SumUp-Code; #628 Teilrefund 64.95 – welcher Artikel zurück (Lager)?
- [x] 1. `sumup_client`: Pagination-Fix (Query-String-href, Fehler statt stillem Abbruch), Payout-Einträge roh
- [x] 2. `ledger.py` (reine Rechenlogik, PAYOUT/REFUND_DEDUCTION) + `matching.py` exakt per tx-Code (MATCHED/GAP/DUPLICATE/ONLY_SUMUP/ONLY_SK/REFUND)
- [x] 3. `sync_sumup_payouts` (Auto-Erkennung + Mail). **Cron noch eintragen (Daniel):**
      `45 5,6 * * * /home/daniel/mileja/workbench/run_at_swiss_time.sh 07:45 docker exec stock_keeper_web python manage.py sync_sumup_payouts >> /home/daniel/stock_keeper_cron.log 2>&1`
- [x] 4. `booking.py` Buchungsvorschlag 1101-Methode je Monat + Beleg v2 (`voucher_pdf_v2.html`), Konten via `settings.SUMUP_ACCOUNTING`
- [x] 5. KLARA: Umsatzbuchungen A gebucht – Aug #25126 (31.07.), Sep #25128 (31.08.), Okt #25129 (30.09.), Nov #25130 (31.10.), je Passkey-Freigabe. Bankabgleich (B) + Beleg-Anhang manuell durch Daniel (27.11.)
- [x] 6. KLARA-Bankabgleich Aug–Nov durch Daniel erledigt, 1101 = 0.00 (27.11.)
- [x] 9. Mail mit Freigabe-Link: /reconciliation/<pk>/klara/ bucht per KLARA-REST-API (explizite Berechtigung post_to_klara: admin, ari). Abgleiche #19–#22 mit KLARA-IDs nachgetragen.
- [ ] 10. Erster echter API-POST erst bei der Dezember-Gutschrift (Feldnamen aus KLARA-Antwort abgeleitet, Test-POST war blockiert) → Ergebnis prüfen
- [x] 7. Abschluss nur wenn grün; Aktionen ohne Buchungswirkung entfernt; Nachbuchen verknüpft bestehenden Kassen-Sale statt Duplikat; „Verknüpfen" für Sale-ohne-Code ↔ Zahlung
- [x] 8. Tests `reconciliation/tests.py` (14, Szenarien Aug/Jun/Sep), Suite 50/50 grün; Trockenlauf Jun–Nov: jede Auszahlung Differenz 0.00, Saldo 1101 = 0.00

## Review
- Migration 0004 (additiv) auf PROD angewendet, `docker restart stock_keeper_web`, Login 200.
- Aug (#19) grün: Laden 3475.77, Refund 64.95 (3800), Gebühren 58.82 → 3352.00.

## Offene Entscheide
- 1101-Durchlaufkonto (empfohlen) vs. weiter „alles in einer Bankbuchung" wie bisher
- Wer gibt frei (Alessia / Daniel)? Treuhänder/Revisor mit 1101-Methode einverstanden?
- Ab wann neue Methode: rückwirkend ab Aug-Payout (alle 4 offen) – sinnvoll

---

# SSO/Authentik – Passwort-Login abschaffen + Logout via Authentik (2026-10-27)

Entscheid Daniel: Logout **komplett** (inkl. Authentik), Login-Seite **nur SSO-Button** + Notfallzugang.

## (1) Login nur noch via SSO
- [x] login.html: nur SSO-Button (mit `next`), Passwortformular nur bei `/admin/login/?local=1`
- [x] `ModelBackend` → `core.auth_backends.SuperuserOnlyModelBackend` (Passwort nur für Superuser, Permissions bleiben für alle)
- [x] "Passwort vergessen"-Link entfernt
- [x] Eingeloggt-aber-nicht-berechtigt: Hinweis + Logout-Button (sonst SSO-Schleife auf denselben User)

## (2) Logout über Authentik
- [x] `LOGOUT_REDIRECT_URL = OIDC_OP_LOGOUT_ENDPOINT` → Jazzmin-Logout (POST/CSRF) endet bei Authentik end-session
- [x] toten GET-View `core/views_auth.sso_logout` + URL entfernt
- [x] Authentik: Provider `stockkeeper` → `default-invalidation-flow` (von Daniel umgestellt)

## Verifikation
- [x] 6 neue Tests (core/tests.py `SSOOnlyLoginTests`), gesamte Suite 36/36 grün
- [ ] Prüfen, dass mind. ein Superuser mit bekanntem Passwort existiert (Notfallzugang)
- [x] `docker restart stock_keeper_web` (27.10.), Live-Check: kein Passwortfeld, `?local=1` zeigt Formular, CSS ausgeliefert
- [x] Befund 27.10.: Authentik-end-session zeigt trotz `default-invalidation-flow` Auswahlseite (SessionEndStage fest im Code, 2024.12)
- [x] `LOGOUT_REDIRECT_URL` → `/if/flow/stockkeeper-logout/` (Code + Test, noch nicht deployed)
- [ ] Authentik: Flow `stockkeeper-logout` anlegen (Logout-Stage + Redirect-Stage → /admin/login/)
- [ ] Restart + Browser-Test Logout → SSO fragt erneut nach Passwort

## Review
- Bewusst ohne Auto-Redirect auf Authentik (Wunsch: SSO-Button behalten).
- "Passwort ändern" im Jazzmin-Usermenü per CSS ausgeblendet (custom_admin.css).

---

# SumUp Reconciliation – Differenz CHF 2.19 (Payout pk=9)

## Befund
- Sale #398 / Txn TAAA2U3T2UX: SumUp-Teilrückerstattung 2.19 am 17.05. (Refund TAAA232GAYQ),
  vom Juni-Payout abgezogen. Tatsächlich abgerechnet 236.61 brutto statt 238.80.
  SK Sale #398 wurde nie rückerstattet → weiterhin 238.80.
- Matching vergleicht sk_amount vs txn.amount (beide 238.80) → kein GAP erkannt.
- Report validiert Schlusssumme (Laden+Café−Gebühren) nie gegen Bankgutschrift.

## Massnahmen
- [x] Matching nutzt tatsächlich abgerechneten Betrag (Payout netto+Gebühr) als sumup_amount
- [x] Zentrale `SumUpPayout.booking_summary` (DRY für View + PDF) inkl. net_delta + refund_lines
- [x] Review-Screen: rotes Warnbanner bei Differenz/Teilrückerstattung
- [x] PDF: net_delta-Prüfung + Hinweis auf Teilrückerstattungen
- [x] Payout 9 neu abgleichen (Live-Daten korrigieren)
- [x] Container neu starten, Commit + Push, Analyse-Mail an admin@mileja.ch
