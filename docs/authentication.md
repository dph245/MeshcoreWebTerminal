# Repeater- und Roomserver-Anmeldung

## API-Prüfung (2026-10-13)

Geprüft wurden die installierte Python-Library `meshcore==2.3.14` sowie die
Upstream-Firmware auf `main` (keine Hardwareverbindung):

- [BaseChatMesh.cpp](https://github.com/meshcore-dev/MeshCore/blob/main/src/helpers/BaseChatMesh.cpp),
  `sendLogin`: erzeugt und sendet ein Paket; berechnet die geschätzte Antwortzeit
  anhand Airtime und DIRECT-/FLOOD-Route. Kein eigener Login-Retry.
- [Companion MyMesh.cpp](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp),
  `CMD_SEND_LOGIN`, `onContactResponse`, `CMD_RESET_PATH`: `MSG_SENT` enthält
  Routingtyp und `suggested_timeout` in Millisekunden. Eine gemeinsame
  `pending_login`-Variable ordnet die Antwort zu. Erfolg/Ablehnung kommen als
  asynchrone Push-Ereignisse. Reset setzt den Pfad auf unbekannt, ohne Funkpaket.
- SDK `commands/messaging.py`: `send_login` ruft nur `_send_login_raw` auf;
  keine Wiederholungen. `send_login_sync` wartet auf Erfolg, behandelt die
  gewünschte Ablehnungs-/Retrylogik aber nicht. Die Bridge bleibt deshalb beim
  asynchronen Kommando mit eigenem zentralem Zustandsautomaten.

Room-Keepalive und Synchronisation nach erfolgreichem Login sind davon unabhängig.
Eine konkrete installierte Firmwareversion wurde nicht aus einem Gerät ausgelesen.

## Verhalten

`start_login`, `send_login_attempt` und `retry_login` gelten für Repeater-CLI,
Roomserver-CLI und Room-Chat. Passwörter existieren nur im laufenden Aufruf/Task,
nicht in SQLite, Snapshots oder Monitor-Ereignissen.

- Höchstens drei Versuche insgesamt, niemals drei Versuche je Routingmodus.
- Antwortzeit: `suggested_timeout * 1.5`, mindestens 30 und höchstens 300 Sekunden;
  bei fehlender/ungültiger Schätzung 60 Sekunden. Liegt schon die Firmware-Schätzung
  über 300 Sekunden, beendet die Obergrenze den Vorgang ohne erneuten Versand.
- Nur nach bestätigtem `MSG_SENT` und ausbleibender Loginantwort wiederholen.
  Fehler oder fehlende lokale Companion-Bestätigung beenden den Vorgang;
  der Sendestatus könnte in diesem Fall unbekannt sein.
- AUTO liest den echten Companion-Pfad. Nach erfolglosem DIRECT-Versand mit
  unverändertem Pfad und ohne zwischenzeitliches PATH_UPDATE ist einmalig ein
  bestätigter Reset mit FLOOD-Fallback erlaubt. Ein neu gelernter Pfad bleibt erhalten.
- Höchstens ein FLOOD-Versuch pro Anmeldung. Bleibt anschließend der Pfad unbekannt,
  wird beendet. Ein inzwischen gelernter DIRECT-Pfad kann die übrigen Versuche nutzen.
- MANUAL aktiviert und prüft für jeden Versuch die konfigurierte DIRECT-Route;
  kein automatischer Flood-Fallback.
- Erfolg und ausdrückliche Ablehnung stoppen sofort. Schweigt ein Server bei
  falschem Passwort, lässt sich das protokollbedingt nicht von Verlust unterscheiden.
- Wegen des einzigen Firmware-Loginplatzes läuft global höchstens eine Anmeldung.
  Routenänderung und Kontaktlöschung für das Ziel sind währenddessen gesperrt.
  Der Bridge-Lock schützt lokale Kommandosequenzen; während der Wartezeit bleibt
  Empfang/Monitoring möglich. Disconnect und Room-Logout beenden den Task.

`AUTH_LOGIN` zeigt Versuch, konfigurierten Modus, tatsächlich gemeldetes Routing,
Wartezeit und Ergebnis in der vorhandenen Netzmonitor-Tabelle. Unbestätigte
Sendekommandos tragen Routing `unknown`. Der Filter „Anmeldungen“ zeigt nur diese
Ereignisse; technische Schlüssel bleiben in den Details.

## Protokollgrenzen und Tests

Eine Antwort innerhalb derselben laufenden Anmeldung gilt auch dann, wenn sie zu
einem früheren Versuch gehört. Nach Abschluss wird sie ignoriert. Das Protokoll
liefert keinen eindeutigen Login-Versuchsschlüssel: Eine sehr späte Antwort aus
einer früheren, abgeschlossenen Anmeldung lässt sich nach einer **neu gestarteten**
Anmeldung desselben Geräts nicht sicher unterscheiden. Auch eine Pfadänderung
innerhalb der Firmware zwischen Rücklesen und Sendekommando ist nicht atomar
verhinderbar. Gemeldetes DIRECT/FLOOD ist keine Bestätigung der exakten Hopfolge.

`tests/test_auth.py` simuliert Antworten, Timeouts, frühe und verspätete Pushes,
Routingwechsel, Rücklesefehler, konkurrierende Aufrufe und Disconnects für alle
drei Zugangsarten. Browser-Tests prüfen Route und Monitor mit abgefangenem HTTP.
Alle Tests laufen ohne echte Funkpakete und ohne produktive Companion-Verbindung.

Validierung: **256 Unit-Tests bestanden**, dazu Browserprüfungen für Routing,
Netzmonitor, Repeater und Rooms. `git diff --check` sauber. Die vorhandene
Starlette/httpx-Deprecation-Warnung bleibt unverändert.
