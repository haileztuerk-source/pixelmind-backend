# Live-Kurse aus deiner Handelsplattform

Der Terminal läuft auf einem Server und kommt dort nur an öffentliche
Kursquellen — und die sind 15 Minuten verzögert. Deinen echten
USTEC-Kurs hat nur dein Broker.

Diese Brücke holt ihn dort ab, wo du ihn schon hast: aus MetaTrader 5 auf
deinem eigenen Rechner. Sie schiebt Kerzen und Kurse zum Terminal,
solange sie läuft.

## Warum nicht direkt auf dem Server?

Die Python-Anbindung von MT5 spricht mit dem MT5-**Programm** auf
demselben Rechner, nicht über das Netz. Auf einem Server ohne MT5 gibt es
nichts zu lesen. Deshalb geht die Brücke andersherum: sie läuft bei dir
und meldet sich beim Server.

Bei GBE ist das der einzige Weg — GBE bietet MT4, MT5 und TradingView,
aber keine offene Kurs-Schnittstelle. Wechselst du später zu FTMO, geht es
auch ohne deinen PC: FTMO hat cTrader, und die cTrader Open API ist
kostenlos.

## Einrichten

**1. Auf dem Server** (Render → Environment) ein Geheimnis setzen:

```
LIVE_TOKEN = eine-lange-zufaellige-zeichenfolge
```

Ohne das nimmt der Server nichts an. Er steht unter einer öffentlichen
Adresse — ohne Prüfung könnte jeder Kerzen hineinschreiben und damit den
Chart fälschen, auf den du einen Einstieg setzt.

**2. Auf deinem PC** einmalig:

```
pip install MetaTrader5 requests
```

**3. Starten** (MT5 muss offen und angemeldet sein):

```
set TERMINAL_URL=https://deine-adresse.onrender.com
set LIVE_TOKEN=dieselbe-zeichenfolge-wie-oben
set MT5_SYMBOL=USTEC
python mt5_bridge.py
```

Unter Linux oder macOS `export` statt `set`.

Heißt das Symbol bei deinem Broker anders, sagt dir die Brücke beim Start,
welche Namen sie gefunden hat.

## Woran du siehst, dass es läuft

Im Kopf des Terminals steht **LIVE** statt *Cboe*, und neben dem
Instrumentnamen leuchtet ein grüner Punkt. Antippen zeigt, wie alt der
letzte Tick ist.

Bleibt die Brücke 20 Sekunden still, fällt der Terminal auf die
öffentliche Quelle zurück, der Punkt wird grau und im Kopf steht wieder
die Verzögerung. Er tut nie so, als wäre ein alter Kurs aktuell — ein
eingefrorener Live-Kurs ist gefährlicher als ein ehrlich verzögerter.

## Was die Brücke tut und was nicht

Sie liest **nur Kurse**: Minutenkerzen und den letzten Tick. Sie liest
keine Kontodaten, keine Positionen, keine Orders, und sie kann nicht
handeln.

## Preisräume

Der Terminal rechnet im Index-Preisraum — dort stehen die Optionsketten,
die Wände, das Zero-Gamma. Dein CFD steht daneben: er folgt dem Future und
trägt dessen Basis.

Läuft die Brücke, misst der Terminal diese Basis selbst und laufend — und
zwar **synchron**: er vergleicht deinen Kurs nicht mit dem 15 Minuten
alten Indexkurs (das wäre die Basis plus eine Viertelstunde
Marktbewegung), sondern mit dem Kurs, den dein Instrument in genau jener
Minute hatte.

Gerechnet und gespeichert wird weiter im Index-Preisraum; der Versatz
wirkt nur dort, wo ein Preis zu Text wird. Deshalb bleiben Abstände —
ATR, Zonenbreiten, "42 Punkte entfernt" — unverändert: in jeder Differenz
kürzt sich der Versatz heraus.

Fällt die Brücke aus, bleibt der zuletzt gemessene Versatz stehen. Sonst
sprängen alle Zahlen um mehrere zehn Punkte, ohne dass sich der Markt
bewegt hätte.
