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
| `news.py` | Wirtschaftskalender und gefilterte Schlagzeilen |
| `agent.py` | 8 Ausloeser mit Abklingzeiten, Tagesplan, Budget-Waechter |
| `server.py` | Flask, Endpunkte, Hintergrund-Scanner |
| `static/index.html` | Handy-Oberflaeche im Redesign-Look vom 03.09. |

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
```

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
  Chat-Verlauf und Tagesplan beim Neustart.
