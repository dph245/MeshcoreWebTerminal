# Manueller TRACE

Im Mesh-Bereich den Unter-Tab „Manueller TRACE“ wählen, Repeater-Kontakte auswählen oder Hex-IDs
(Public-Key-Präfixe) eingeben. Hops lassen sich hinzufügen, entfernen und mit den
Pfeilen umsortieren. Wiederholungen bleiben erhalten. „Rückweg spiegeln“ erweitert
beispielsweise A → B → C zu A → B → C → B → A: Der Wendepunkt wird einmal besucht.
Der vollständige Pfad wird vor dem Senden angezeigt. Ohne diese Option muss der
Benutzer selbst einen Pfad wählen, dessen letzter Repeater den Companion erreicht.
Der Companion gehört nicht in die Liste der Repeater-IDs.

Nur „TRACE einmal senden“ ruft die vorhandene Companion-Verbindung auf. Bearbeiten,
Öffnen, Timeout, Neuladen und Wiederverbinden lösen keine TRACE-Sendung aus. Es gibt
keine Wiederholung, periodische Messung, automatische Kontaktabfrage oder Pfadsuche.

Jede Messung erhält eine eigene ID, einen eigenen 32-Bit-Tag und einen Zeitstempel.
SQLite speichert den vollständigen gesendeten Pfad, Namen zum Messzeitpunkt,
Companion-Adresse, Status und Messwerte. Gleiche Pfade überschreiben sich nicht.
Die aufklappbaren Ergebnisse zeigen SNR beim jeweiligen empfangenden Repeater in
Pfadreihenfolge; die letzte Zeile ist der Empfang am Companion. „Fehlt“ bedeutet
kein Messwert, nicht 0 dB. Bei ausbleibender Rückkehr gibt es normalerweise überhaupt
keine Hop-Messwerte: Das Protokoll liefert keine laufenden Zwischenberichte.

Antworten werden anhand Tag, Auth-Feld, Flags und geordneter Hash-Liste zugeordnet.
Mehrfache Besuche desselben Repeaters werden nach Position ausgewertet. Verspätete
Antworten ergänzen ihre ursprüngliche Messung, auch nach einem Timeout. Doppelte
Antworten überschreiben das gespeicherte Ergebnis nicht. Verbindungsabbruch und
Neustart markieren wartende Messungen als unterbrochen; sie starten sie nicht neu.
Ein fehlendes Sendebestätigungsereignis lässt offen, ob bereits gesendet wurde.

## API-Prüfung und Grenzen

Geprüft wurde die fest installierte Library **meshcore 2.3.14**:

- `meshcore/commands/messaging.py`: `send_trace(auth_code, tag, flags, path)`
  verwendet den vorhandenen Companion-Befehl `CMD_SEND_TRACE_PATH` (36).
- `meshcore/reader.py`: `TRACE_DATA` (0x89) liefert `tag`, `auth`, `flags`,
  `path_len`, geordnete `{hash, snr}`-Einträge und den abschließenden Companion-SNR.
  Die Library rechnet signierte SNR-Bytes bereits in dB um (geteilt durch 4).
- Das bestehende `Bridge.command()` serialisiert die Befehlsausführung zusammen
  mit dem bestehenden Bridge-Lock. DISCOVER und der bisherige TRACE-Ereignislog
  bleiben erhalten. Es gibt keine eigene Paketimplementierung oder Firmwareänderung.

Offizielle Quellen (geprüft am 09.10.2026):

- [Companion-Protokoll](https://github.com/meshcore-dev/MeshCore/wiki/Companion-Radio-Protocol)
- [Companion: Sendebefehl und TRACE-Antwort](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp)
- [Weiterleitung und TRACE-Payload](https://github.com/meshcore-dev/MeshCore/blob/main/src/Mesh.cpp)
- [Paket-Hash einschließlich TRACE-Hopposition für wiederholte Besuche](https://github.com/meshcore-dev/MeshCore/blob/main/src/Packet.cpp)
- [Protokollkonstanten](https://github.com/meshcore-dev/MeshCore/blob/main/src/MeshCore.h)
- [Companion-Framegröße](https://github.com/meshcore-dev/MeshCore/blob/main/src/helpers/BaseSerialInterface.h)

TRACE nutzt eigene Hash-Längen von **1, 2, 4 oder 8 Bytes**, codiert durch die
untersten zwei Flag-Bits; diese unterscheiden sich von normalen Mesh-Pfaden.
Mehrbyte-TRACE setzt entsprechende Firmwareunterstützung voraus (Quelltext: v1.11+).
Alle IDs eines Pfads müssen dieselbe Länge haben. Ein bekannter vollständiger
Repeater-Schlüssel wird auf diese Länge gekürzt. Bei mehreren passenden bekannten
Schlüsseln wird kein eindeutiger Name behauptet. Auch bei Auswahl eines Kontakts
adressiert das Funkprotokoll nur das Präfix: Hash-Kollisionen bleiben möglich.

Die App begrenzt inklusive Rückweg auf **63 / 53 / 31 / 17 Hops** für 1 / 2 / 4 / 8
Bytes. Validiert werden der SNR-Pfad (<64 Einträge beim Empfang), 184 Payload-Bytes
abzüglich 9 TRACE-Headerbytes und konservativ 172 Bytes für die Companion-Antwort
(13 + Hops × (Hash-Länge + 1)). Aktuelle Firmware hat einen größeren Frame-Puffer;
das konservative Budget vermeidet, diesen für ältere Companions vorauszusetzen.
Leere Pfade werden abgelehnt, damit die Library keinen Ersatz-Hop 00 erzeugt.

`POST /api/trace`: `{"path":["ab","cd"],"hash_size":1,"return_path":true}`.
Wie andere schreibende Endpunkte benötigt dies `X-Meshcore-Client: web`.
Validierungsfehler senden nichts. Nicht unterstützte Befehle werden als Fehler
angezeigt und gespeichert; es gibt keinen alternativen Funkmechanismus.

## Tests ohne Funkverkehr

```sh
.venv/bin/python -m pytest -q
.venv/bin/python tests/browser_trace.py
```

Die Tests verwenden simulierte Companion-Antworten und abgefangene Browseranfragen.
Ein End-to-End-Test mit realen Repeatern wurde nicht durchgeführt.
