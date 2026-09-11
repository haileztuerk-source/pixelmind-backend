@echo off
rem ====================================================================
rem  ChartTerminal - Live-Kurse aus MetaTrader 5
rem
rem  Doppelklick genuegt. Beim ersten Mal fragt das Fenster drei Dinge
rem  ab und merkt sie sich in bruecke.ini daneben; danach startet es
rem  ohne Rueckfrage.
rem
rem  Vorher muss MetaTrader 5 offen und angemeldet sein - die Bruecke
rem  liest das Programm aus, sie meldet sich nicht selbst an.
rem ====================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
title ChartTerminal - Bruecke

echo.
echo   ============================================
echo     ChartTerminal - Live-Kurse aus MT5
echo   ============================================
echo.

rem ---- Python finden ------------------------------------------------
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY python --version >nul 2>&1 && set PY=python
if not defined PY (
  echo   Python wurde nicht gefunden.
  echo.
  echo   Hole es von python.org und setze bei der Installation den
  echo   Haken bei "Add Python to PATH". Danach diese Datei erneut
  echo   doppelklicken.
  echo.
  pause
  exit /b 1
)

rem ---- Pakete sicherstellen -----------------------------------------
%PY% -c "import MetaTrader5, requests" >nul 2>&1
if errorlevel 1 (
  echo   Einmalige Einrichtung, das dauert einen Moment...
  %PY% -m pip install --quiet --disable-pip-version-check MetaTrader5 requests
  if errorlevel 1 (
    echo.
    echo   Die Pakete liessen sich nicht installieren.
    echo   Versuche es von Hand:  pip install MetaTrader5 requests
    echo.
    pause
    exit /b 1
  )
  echo   Fertig.
  echo.
)

rem ---- Einstellungen: beim ersten Mal erfragen ----------------------
if not exist bruecke.ini (
  echo   Erster Start - drei Angaben, dann nie wieder.
  echo.
  echo   1^) Die Adresse deines Terminals.
  set /p U="      [https://chartterminal.onrender.com] : "
  if "!U!"=="" set U=https://chartterminal.onrender.com
  echo.
  echo   2^) Das Geheimnis. Genau der Wert, der bei Render unter
  echo      Environment als LIVE_TOKEN steht.
  set /p T="      LIVE_TOKEN: "
  if "!T!"=="" (
    echo.
    echo   Ohne Geheimnis nimmt der Server nichts an. Abgebrochen.
    echo.
    pause
    exit /b 1
  )
  echo.
  echo   3^) Wie heisst der Nasdaq-CFD bei deinem Broker?
  set /p S="      [USTEC] : "
  if "!S!"=="" set S=USTEC
  echo.
  ^> bruecke.ini echo # ChartTerminal - Zugangsdaten der Bruecke.
  ^>^> bruecke.ini echo # Diese Datei enthaelt ein Geheimnis - nicht weitergeben.
  ^>^> bruecke.ini echo TERMINAL_URL=!U!
  ^>^> bruecke.ini echo LIVE_TOKEN=!T!
  ^>^> bruecke.ini echo MT5_SYMBOL=!S!
  echo   Gemerkt in bruecke.ini
  echo.
)

rem ---- Starten ------------------------------------------------------
echo   MetaTrader 5 muss jetzt offen und angemeldet sein.
echo   Beenden mit Strg+C oder Fenster schliessen.
echo.
%PY% mt5_bridge.py

echo.
echo   Die Bruecke ist beendet. Der Terminal faellt in etwa zwanzig
echo   Sekunden auf die verzoegerte Quelle zurueck.
echo.
pause
