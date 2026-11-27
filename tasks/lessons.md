# Lessons

## SumUp Reconciliation — Payout-Abzug ≠ Rückerstattung dieser Transaktion (2026-06-11)

**Fehler:** Bei Payout #9 zeigte Txn `TAAA2U3T2UX` (Sale #398) ein um 2.19 zu
niedriges Payout-Netto. Ich schloss daraus voreilig auf eine „Teilrückerstattung
von #398" und baute Code + verschickte eine Analyse-Mail mit dieser falschen
Ursache. Tatsächlich war #398 nie rückerstattet — die 2.19 ist die nicht
erstattete Bearbeitungsgebühr einer **vollständigen Rückerstattung von #436**
(145.80). SumUp zieht solche Gebühren via `PAYOUT_DEDUCTION` (`paid_for`-Feld)
von der Auszahlungszeile einer **anderen** Transaktion ab.

**Regel:**
- Eine Differenz auf der Auszahlungszeile einer Transaktion gehört nicht
  zwingend zu DIESER Transaktion. Vor Attribution immer die `events` der Txn
  prüfen (`PAYOUT_DEDUCTION` + `paid_for`) und die REFUND-Transaktionen abrufen.
- Wenn der Benutzer ein konkretes Gegenindiz nennt (hier: „im Dashboard nur eine
  Rückerstattung von 145.80"), zuerst dieses Indiz verfolgen, nicht die eigene
  These verteidigen.
- Bei Geldflüssen das Modell gegen die Ground Truth (Bankgutschrift) UND gegen
  die fachliche Plausibilität prüfen — „Summe stimmt" heisst nicht „Attribution
  stimmt": 2.19 lässt sich an beliebiger Stelle abziehen und das Total geht
  trotzdem auf.
- Nicht voreilig ausliefern/mailen, bevor die Ursache an mehreren Datenpunkten
  bestätigt ist (Plan-Mode-Prinzip: bei Unsicherheit erst verifizieren).

## Authentik end-session ≠ direkter Logout (2026-10-27)

**Fehler:** Logout auf `/application/o/<slug>/end-session/` umgeleitet und dem
Benutzer empfohlen, den Invalidation-Flow des Providers umzustellen, ohne den
Endpunkt im Code zu prüfen. Authentik 2024.12 hängt in `EndSessionView` *immer*
eine `SessionEndStage` an (Auswahlseite „Go back / Log out of … / Log back into …“),
unabhängig vom Flow. Die Provider-Einstellung kann das nicht abschalten.

**Regel:**
- Bei Verhalten eines Fremdsystems (Authentik, SumUp, …) die Quelle der
  installierten Version lesen, bevor ich eine Konfiguration als Lösung zusage.
- „Logout funktioniert“ erst behaupten, wenn der End-to-End-Ablauf gesehen ist,
  nicht nur der Django-Redirect.
- Direkter Authentik-Logout: eigener Invalidation-Flow via `/if/flow/<slug>/`
  (User-Logout-Stage + Redirect-Stage), nicht der OIDC end-session-Endpunkt.
