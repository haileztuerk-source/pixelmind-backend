# ChartTerminal Cloud

Die Handy-Fassung des lokalen ChartTerminals. Laeuft ohne MetaTrader und
ohne einen einzigen API-Key.

## Warum das ueberhaupt geht

Die Options- und Gamma-Haelfte des Originals haengt an `cboe_chain.py` —
und der ruft seit jeher nur eine oeffentliche URL auf. Diese Fassung baut
darauf auf und ersetzt nur die eine Schicht, die nicht mitkommt: MT5.

| Schicht | Original | Hier |
|---|---|---|
| Optionskette, OI, Greeks | Cboe-CDN | **unveraendert** |
| Kurse und Bars | MT5, Symbol `USTEC.c` | Cboe-Intraday, Yahoo als Historie |
| News und Kalender | ForexFactory + 3 RSS | **unveraendert** |
| Agent-Texte | Gemini | Gemini, sonst regelbasiert |

## Der Nebeneffekt, der vier Blocker aufloest

Cboe liefert die Minutenbars des **Index** — also denselben Preisraum, in
dem auch die Strikes stehen. Damit gibt es keine Umrechnung mehr, und
damit auch nicht:

- den Basis-Jitter von ±170 Punkten aus `_fit_chart_mapping()`
- die doppelte Kursumrechnung Strike → Future → MT5
- die Frage "welcher NQ?" (Cash 29.179 / CFD 29.246 / Future 29.302)
- das dreifache Zeitzonen-Durcheinander

## Module

| Datei | Aufgabe |
|---|---|
| `cboe.py` | Kette und Intraday-Bars vom Cboe-CDN, mit Stale-Cache |
| `market.py` | Yahoo-Bars, ATR, Volumenprofil, Session-Marken, Bar-Kaskade |
| `gex.py` | Waende, Zero-Gamma, Max Pain, Pin, Vanna, Charm, Verfallsleiter |
| `zones.py` | Konfluenz-Zonen, `MERGE_ATR = 0.28` wie im Original |
| `daybook.py` | Zonenbuch: Level am Tagesanker einfrieren, danach fortschreiben |
| `news.py` | Wirtschaftskalender und gefilterte Schlagzeilen |
| `agent.py` | 8 Ausloeser mit Abklingzeiten, Tagesplan, Budget-Waechter |
| `store.py` | Zustandsspeicher: Postgres wenn `DATABASE_URL` gesetzt, sonst Datei |
| `keepalive.py` | Wachhalten nur waehrend der Handelszeit |
| `server.py` | Flask, Endpunkte, Hintergrund-Scanner |
| `static/index.html` | Handy-Oberflaeche im Redesign-Look vom 03.09. |

## Fixe Level

Ohne Einfrieren wandert jede Wand mit jedem Snapshot — und ein Level, das
sich staendig verschiebt, laesst sich weder handeln noch messen. `daybook.py`
uebernimmt die Mechanik aus `_zone_day_book()`:

- **Anker** um `ANCHOR_HOUR` (Standard 6:00 UTC). Vor dem Anker zaehlt noch
  der Vortag.
- **Einfrieren** von Call-/Put-Waenden samt Leiter, Zero-Gamma, Max Pain und
  Gamma-Pin. Danach bleiben diese Zahlen den Tag ueber stehen.
- **Drift** gegen den laufenden Stand wird mitgefuehrt und im Chart als
  Geisterlinie gezeichnet — man sieht, wohin die Wand seit dem Anker lief.
- **Zustaende** `ungetestet -> im Test -> gehalten | gebrochen`. Bruch ist
  Akzeptanz jenseits der Kante (0,15 ATR), nicht blosse Beruehrung.
- **Anti-Zappel-Regel**: ein an der Kante zappelnder Kurs erzeugt eine
  Beruehrungs-Episode, nicht dreissig (600 s Abstand).
- **Identitaet ueber Tage**: ein Level behaelt seine ID, solange sein Strike
  wiederkehrt. Der Pruefungszaehler laeuft weiter — die Voraussetzung fuer
  "siebter Test" und "nie drin gewesen".
- **Qualitaetssperre**: ist die Kette faul, bleibt das alte Buch stehen,
  statt ein neues zu wuerfeln.

## Bedienung

Aufbau wie eine Social-App, weil ein Telefon einhaendig bedient wird:

- **Bottom-Navigation** mit fuenf Ansichten — Chart, Level, Gamma, Agent,
  Plan. Alles im Daumenbereich, Ziele mindestens 44 px.
- **Wischen** zwischen den Ansichten (Scroll-Snap). Auf dem Chart selbst
  schiebt und zoomt die Geste stattdessen den Kurs.
- **Marktleiste** oben: alle fuenf Maerkte mit Kurs und Tagesveraenderung,
  antippen wechselt. Speist sich aus dem Intraday-Endpunkt (100 KB je Markt)
  statt aus den vollen Ketten (6 MB).
- **Sheets von unten** fuer Markt und Chart-Ebenen statt Menues in der Mitte.
- **Langes Druecken** im Chart blendet ein Fadenkreuz mit OHLC ein.
- Die Werkzeugleiste des Originals hatte 16 Haekchen nebeneinander; hier
  sind es zwei Menues und eine Chip-Reihe.

## Endpunkte

```
GET  /                      Oberflaeche
GET  /health                Status, aktive Maerkte, Budget
GET  /api/markets           NQ · ES · YM · RTY · GC
GET  /api/state?market=NQ&tf=15m
GET  /api/candles?market=NQ&tf=15m
GET  /api/agent/messages?market=NQ
POST /api/agent/chat        {"market":"NQ","text":"..."}
GET  /api/agent/plan?market=NQ
GET  /api/book?market=NQ    fixe Level mit Zustand und Drift
GET  /api/overview          alle Maerkte mit Kurs und Tagesveraenderung
```

## Auf dem Telefon installieren

Es gibt **kein APK** — das hier ist eine Web-App. Nach dem Deployment
verhaelt sie sich aber wie eine installierte App: `manifest.webmanifest`
setzt Vollbild, Icon und Startfarbe.

- **iPhone (Safari):** Teilen -> "Zum Home-Bildschirm"
- **Android (Chrome):** Menue -> "App installieren"

Danach startet sie ohne Browser-Leiste, mit eigenem Icon.

## Lokal starten

```bash
pip install -r requirements.txt
python -m terminal.server          # http://localhost:8770
```

## Auf Render

`render.yaml` bringt den Dienst `chartterminal` mit. **Genau ein Worker** —
sonst laeuft der Beobachtungs-Thread mehrfach und meldet doppelt.

Optional im Dashboard: `GEMINI_API_KEY`. Ohne Key antwortet der Agent aus
Vorlagen; jede Zahl stammt dann zwingend aus der Engine, keine kann
erfunden werden.

## Kosten

Alles kostenlos, ohne Karte, ohne Testphase. Was der Dienst anfasst:

| Bestandteil | Kosten | Grenze |
|---|---|---|
| Cboe-CDN (Ketten, Intraday) | 0 € | kein Key, kein erkennbares Limit |
| Yahoo (Historie) | 0 € | drosselt stossweise mit 429, Ausweichkette faengt das |
| ForexFactory + RSS | 0 € | kein Key |
| Render Web Service | 0 € | 512 MB RAM, 750 Instanzstunden je Account |
| Gemini (optional) | 0 € | dauerhaft freies Kontingent, ~1.500 Aufrufe/Tag |

Gemessener Spitzenverbrauch mit allen fuenf Maerkten im Cache: **158 MB**.
Das passt in die 512 MB des Free-Plans.

### Die Stundenrechnung

750 Instanzstunden gelten fuer den **ganzen Account**, ein Monat hat 744.
Ein einziger rund um die Uhr wachgehaltener Dienst braucht also alles auf -
und `pixelmind-backend` im selben Account ginge leer aus.

Deshalb haelt `keepalive.py` den Dienst nur waehrend der Handelszeit wach
(12-21 Uhr UTC, werktags): rund **198 Stunden im Monat**. Ausserhalb
schlaeft er und wacht beim ersten Aufruf in etwa 50 Sekunden auf.

### Zustand ueber Neustarts hinweg

Der Free-Plan hat keine persistente Festplatte. Ohne Datenbank waeren
Zonenbuch, Chat-Verlauf und Tagesplan bei jedem Neustart weg - und der
Pruefungszaehler ueber Tage waere in Wahrheit "seit dem letzten Neustart".

`store.py` legt den Zustand deshalb in Postgres ab, sobald `DATABASE_URL`
gesetzt ist. Ohne die Variable bleibt es beim Dateiverhalten, damit lokales
Entwickeln keine Datenbank braucht.

**Einrichtung bei Neon** (kostenlos, ohne Karte):

1. neon.com -> Anmelden -> Projekt anlegen
2. Die angebotene Connection String kopieren
   (`postgresql://user:pass@ep-....neon.tech/neondb?sslmode=require`)
3. In Render: Service `chartterminal` -> Environment -> `DATABASE_URL` einfuegen
4. Nach dem Neustart meldet `/health` unter `store` den Eintrag
   `"backend": "postgres"`

Die Tabelle legt der Dienst selbst an - ein einziges Key-Value-Schema:

```sql
CREATE TABLE terminal_state (
  key        TEXT PRIMARY KEY,
  value      JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Neon faehrt kostenlose Instanzen nach kurzer Ruhe herunter und beim
naechsten Zugriff wieder hoch. `store.py` verbindet deshalb je Vorgang
frisch und wiederholt einen ersten Fehlschlag - der Weckvorgang selbst
darf keinen Datenverlust verursachen.

## Was diese Fassung bewusst nicht kann

- **Kein Entry-Timing.** Der Cboe-Feed haengt gemessene 15 bis 16 Minuten
  zurueck. Fuer Zonen und Waende folgenlos, fuers Timing unbrauchbar.
- **Keine gemessenen Vorzeichen.** Calls positiv, Puts negativ ist eine
  Annahme. Ohne Fluss-Klassifikation aus zwei Snapshots ist das
  Dealer-Delta-Notional `F(S)` monoton und hat kein Extremum — deshalb
  wird es hier gar nicht erst als Level ausgewiesen.
- **Kein Gameplan.** Rollen, Namensgrammatik und das Level-Buch ueber
  Wochen sind im Original geplant, hier nicht gebaut.
- **Kein Backtest, keine TerminalMap, keine Strike-Map.**
- **Zustand ist fluechtig.** Auf dem Free-Plan verliert der Container
  Chat-Verlauf, Zonenbuch und Tagesplan beim Neustart. Damit faellt auch
  der Pruefungszaehler ueber Tage — er braucht einen persistenten Speicher,
  um sein Versprechen zu halten.
