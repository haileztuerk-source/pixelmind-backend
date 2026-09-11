"""Bruecke: MetaTrader 5 auf deinem PC -> Terminal im Netz.

WOZU

Der Terminal laeuft auf einem Server und kommt dort nur an oeffentliche
Kursquellen - und die sind 15 Minuten verzoegert. Deinen echten
USTEC-Kurs hat nur dein Broker, und den bekommst du in MetaTrader 5.

MT5 laesst sich nicht auf einem Server betreiben: die Python-Anbindung
spricht mit dem MT5-Programm auf demselben Rechner, nicht ueber das
Netz. Also geht die Bruecke andersherum - dieses Programm laeuft bei
dir, liest MT5 aus und schiebt Kerzen und Kurse zum Terminal.

Solange es laeuft, ist der Hauptchart auf deinem Handy dein eigener
Broker-Chart, live. Sobald du es beendest, faellt der Terminal nach
etwa zwanzig Sekunden auf die oeffentliche Quelle zurueck und sagt es
in der Kopfzeile. Er tut nie so, als waere ein alter Kurs aktuell.

WAS DU BRAUCHST

  1. MetaTrader 5 auf diesem Rechner, bei deinem Broker angemeldet.
     Das Programm muss offen sein - die Bruecke liest es aus.
  2. Python 3, und einmalig:  pip install MetaTrader5 requests
  3. Die Adresse deines Terminals und das gemeinsame Geheimnis.

SO STARTEST DU

  set TERMINAL_URL=https://deine-adresse.onrender.com
  set LIVE_TOKEN=dasselbe-geheimnis-wie-auf-dem-server
  set MT5_SYMBOL=USTEC
  python mt5_bridge.py

Unter Linux oder macOS statt `set` ein `export`.

DAS GEHEIMNIS

Der Terminal steht unter einer oeffentlichen Adresse. Ohne Pruefung
koennte jeder Kerzen hineinschreiben und damit den Chart faelschen, auf
den du einen Einstieg setzt. Deshalb nimmt der Server nur an, was das
Geheimnis mitbringt - dasselbe, das dort als LIVE_TOKEN gesetzt ist.
Denk dir eine lange zufaellige Zeichenfolge aus und trag sie an beiden
Stellen ein.

Die Bruecke schickt ausschliesslich Kurse. Sie liest keine Kontodaten,
keine Positionen, keine Orders, und sie kann nicht handeln.
"""

import os
import sys
import time

try:
    import MetaTrader5 as mt5
except ImportError:
    sys.exit("Fehlt: pip install MetaTrader5   (nur unter Windows verfuegbar)")
try:
    import requests
except ImportError:
    sys.exit("Fehlt: pip install requests")


def _einstellungen():
    """Liest bruecke.ini neben diesem Skript, falls vorhanden.

    Der Weg ueber `set TERMINAL_URL=...` in der Eingabeaufforderung
    funktioniert, aber er ist die Huerde, an der die Bruecke haengen
    blieb: drei Zeilen tippen, bei jedem Start neu, und ein Tippfehler
    im Geheimnis meldet sich erst als "abgewiesen". Mit einer Datei
    daneben wird daraus ein Doppelklick - START-BRUECKE.bat fragt die
    Werte einmal ab und schreibt sie hierhin.

    Umgebungsvariablen haben Vorrang, damit der alte Weg weiter gilt.
    """
    pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bruecke.ini")
    werte = {}
    try:
        with open(pfad, encoding="utf-8-sig") as f:
            for zeile in f:
                zeile = zeile.strip()
                if not zeile or zeile.startswith("#") or "=" not in zeile:
                    continue
                k, _, v = zeile.partition("=")
                werte[k.strip().upper()] = v.strip()
    except OSError:
        pass
    return werte


_INI = _einstellungen()


def _wert(name, standard=""):
    return os.environ.get(name) or _INI.get(name) or standard


URL = _wert("TERMINAL_URL", "http://127.0.0.1:8770").rstrip("/")
TOKEN = _wert("LIVE_TOKEN", "")
SYMBOL = _wert("MT5_SYMBOL", "USTEC")
MARKET = _wert("MARKET", "NQ")

TICK_EVERY = 2.0        # Sekunden zwischen zwei Kursmeldungen
BARS_EVERY = 60.0       # Sekunden zwischen zwei Kerzenlieferungen
BARS_COUNT = 1500       # Minutenkerzen je Lieferung - gut 25 Stunden

HEAD = {"X-Live-Token": TOKEN, "Content-Type": "application/json"}


def post(path, payload):
    """Sendet und meldet Fehler, ohne die Bruecke abbrechen zu lassen.

    Eine kurze Netzstoerung darf keine Bruecke beenden, die den ganzen
    Handelstag laufen soll - der Terminal faellt in der Zwischenzeit
    von selbst auf die oeffentliche Quelle zurueck.
    """
    try:
        r = requests.post(f"{URL}{path}?market={MARKET}", json=payload,
                          headers=HEAD, timeout=12)
        if r.status_code == 403:
            print("  abgewiesen: falsches oder fehlendes LIVE_TOKEN")
            return None
        if not r.ok:
            print(f"  {r.status_code}: {r.text[:120]}")
            return None
        return r.json()
    except Exception as exc:
        print(f"  Netz: {exc}")
        return None


def main():
    if not TOKEN:
        sys.exit("LIVE_TOKEN fehlt - ohne Geheimnis nimmt der Server nichts an.")

    if not mt5.initialize():
        sys.exit(f"MT5 antwortet nicht: {mt5.last_error()}\n"
                 "Laeuft das Programm und bist du angemeldet?")

    if not mt5.symbol_select(SYMBOL, True):
        avail = [s.name for s in (mt5.symbols_get() or [])
                 if "TEC" in s.name.upper() or "NAS" in s.name.upper()
                 or "100" in s.name]
        mt5.shutdown()
        sys.exit(f"Symbol {SYMBOL} nicht gefunden.\n"
                 f"Bei deinem Broker heisst es vielleicht: {', '.join(avail[:12]) or '(nichts gefunden)'}\n"
                 "Dann: set MT5_SYMBOL=<name>")

    info = mt5.symbol_info(SYMBOL)
    print(f"Bruecke laeuft: {SYMBOL} ({info.description if info else ''})")
    print(f"  -> {URL}  (Markt {MARKET})")
    print("  Beenden mit Strg+C\n")

    last_bars = 0.0
    while True:
        try:
            now = time.time()

            # Kerzen: die Historie, damit der Chart vollstaendig ist
            if now - last_bars >= BARS_EVERY:
                rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1,
                                                0, BARS_COUNT)
                if rates is not None and len(rates):
                    bars = [{"t": int(r["time"]), "o": float(r["open"]),
                             "h": float(r["high"]), "l": float(r["low"]),
                             "c": float(r["close"]),
                             "v": float(r["tick_volume"])} for r in rates]
                    res = post("/api/live/bars", {"symbol": SYMBOL, "bars": bars})
                    if res:
                        print(f"[{time.strftime('%H:%M:%S')}] "
                              f"{len(bars)} Kerzen -> Server hat {res.get('bars')}")
                    last_bars = now

            # Tick: der letzte Kurs, damit die laufende Kerze lebt
            t = mt5.symbol_info_tick(SYMBOL)
            if t and t.bid and t.ask:
                mid = (t.bid + t.ask) / 2.0
                post("/api/live/tick",
                     {"symbol": SYMBOL, "price": mid, "ts": int(t.time)})

            time.sleep(TICK_EVERY)

        except KeyboardInterrupt:
            print("\nBruecke beendet. Der Terminal faellt gleich auf die "
                  "oeffentliche Quelle zurueck.")
            break
        except Exception as exc:
            print(f"  Fehler: {exc}")
            time.sleep(5)

    mt5.shutdown()


if __name__ == "__main__":
    main()
