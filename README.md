# MeshCore Web Terminal

Lokaler Webclient für einen MeshCore TCP-Companion auf **192.168.88.14:5000**.

- Vorhandene Channels und Chat-Kontakte vom Companion laden
- Hashtag-Channels im Companion hinzufügen und entfernen, einschließlich Rückleseprüfung
- Channel-Nachrichten und Direktnachrichten empfangen und senden
- Nachrichtenverlauf in SQLite, auch nach Neustarts; ältere Nachrichten nachladen
- Empfangspfad unter eingehenden Nachrichten, mit Hop-Anzahl und verfügbaren Knoten-Hashes
- DM-Empfangsbestätigungen (ACK), Sendefehler und Verbindungsstatus
- Netzmonitor mit Live-Funkereignissen, RSSI/SNR, Gerätestatistik und Ereignisfiltern
- Automatische Neuverbindung; responsive deutsche Oberfläche
- Standard-Scope im Companion und gespeicherte Scope-Auswahl pro Channel

## Start mit Docker

```sh
docker compose up -d --build
```

Dann **http://localhost:8090** öffnen. Der Server verbindet sich automatisch mit dem Companion. Der Docker-Host muss `192.168.88.14:5000` erreichen können. Andere Clients sollten die TCP-Verbindung des Companions freigeben.

```sh
docker compose logs -f web
docker compose down
```

Nachrichten bleiben im Volume `meshcore-data` erhalten. `down -v` würde dieses Volume löschen.

## Lokal ohne Docker

Python 3.11 oder neuer:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8090
```

Nur einen Serverprozess verwenden: Er hält die gemeinsame Verbindung zum Companion. Browser erhalten Updates über Server-Sent Events; ausgehende Nachrichten laufen über HTTP. Die Oberfläche braucht keinen Node-Build und lädt keine externen Ressourcen.

## Konfiguration

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `MESHCORE_HOST` | `192.168.88.14` | Companion-IP oder Hostname |
| `MESHCORE_PORT` | `5000` | TCP-Port |
| `MESHCORE_AUTOCONNECT` | `1` | `0`: erst per Button verbinden (lokaler Python-Start) |
| `MESHCORE_DB` | `data/messages.sqlite3` | Nachrichtendatenbank |
| `WEB_PORT` | `8090` | Lokaler Port bei Docker Compose |

Für Docker können Host und Port in einer `.env` gesetzt werden. Compose veröffentlicht den Webserver standardmäßig nur auf dem lokalen Rechner. Für Zugriff anderer Geräte im vertrauenswürdigen LAN die Portbindung bewusst auf `8090:8080` ändern. Die Anwendung hat keine Benutzeranmeldung; alle Browser teilen Nachrichten und Funkzugriff. Nicht ungeschützt ins Internet veröffentlichen.

## Verhalten und Grenzen

Im Netzmonitor lässt sich der **Standard-Scope** des Companions lesen, ändern und mit „Ohne Scope“ löschen. Der Name wird mit `#` normalisiert und darf einschließlich `#` maximal 30 UTF-8-Bytes enthalten. Der Standard gilt für Flood-Verkehr, auch DMs ohne bekannte Route und Adverts; direkte Routen werden dadurch nicht verändert.

In jedem Channel stehen **Companion-Standard**, **Ohne Scope** und **Eigene Region** zur Auswahl. Mit „Speichern“ wird die Auswahl lokal in SQLite hinterlegt und von allen Browsern geteilt. Beim Senden wird der Channel-Scope gesetzt und anschließend zurückgesetzt, damit er nicht auf andere Channels oder DMs übergreift. Wenn die Rücksetzung nicht bestätigt wird, blockiert die Anwendung weitere Sendungen bis zur Neuverbindung. Empfangene Nachrichten werden durch diese Auswahl nicht gefiltert. Die Bedienfelder hängen von den Firmware-Fähigkeiten ab; unbekannte Standard-Scope-Werte werden als nicht verfügbar angezeigt.

Die Protokollrahmen folgen der [Companion-Firmware](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp). Der Adapter behandelt UTF-8-Padding und das Löschen des Standard-Scopes selbst, da `meshcore` 2.3.14 diese Fälle nicht korrekt kodiert.

- Über „Channels verwalten“ lassen sich Hashtag-Channels hinzufügen und vom Companion entfernen. Namen erhalten automatisch ein führendes `#`, bleiben ansonsten einschließlich Groß-/Kleinschreibung erhalten und dürfen maximal 31 UTF-8-Bytes umfassen. Der Schlüssel wird gemäß MeshCore aus den ersten 16 Bytes von SHA-256 des vollständigen Namens abgeleitet. Belegte Plätze werden nicht überschrieben; beim Entfernen werden Name und Schlüssel auf dem Companion geleert. Jede Änderung wird zurückgelesen. Private Channels und Kontakt-Import sind nicht Bestandteil dieser Verwaltung.
- Beim Entfernen oder erkannten Austausch eines Channels wird dessen lokaler Verlauf in SQLite archiviert und die lokale Scope-Auswahl entfernt. Archivierte Nachrichten werden nicht im Verlauf eines neu belegten Channel-Platzes angezeigt; eine Archivansicht gibt es noch nicht. Tests der Verwaltung verwenden ausschließlich simulierte Companion-Schreibvorgänge.
- Empfangen werden neue und noch im Companion gepufferte Nachrichten. Eine frühere Historie anderer Clients kann nicht nachträglich abgerufen werden.
- Empfangspfade werden zusammen mit neuen Nachrichten gespeichert. Bei Channels ergänzt die Bibliothek die Knotenfolge aus passenden entschlüsselten Funklogs, soweit diese während der Verbindung empfangen wurden. Eindeutige Kontakt-Präfixe werden zusätzlich als Namen angezeigt; mehrdeutige oder unbekannte bleiben als Hash sichtbar. DMs liefern häufig nur die Hop-Anzahl oder „Direct-Routing“ ohne Knotenfolge. Direct-Routing ist nicht gleichbedeutend mit null Hops. Fehlen Funklogs oder wurden Nachrichten vor dieser Erweiterung gespeichert, zeigt die Oberfläche die verfügbaren Angaben bzw. „nicht verfügbar“. Bestehende Datenbanken werden ohne Verlust des Verlaufs erweitert.
- „An Companion übergeben“ bestätigt die Übergabe an das Gerät. Nur ein DM-ACK führt zu „Zugestellt“. Channel-Nachrichten haben keine individuelle Empfangsbestätigung.
- Ausgehende Texte sind auf konservative 160 UTF-8-Bytes begrenzt. Umlaute und Emojis können mehrere Bytes belegen. Bei unklarem Sendestatus erfolgt kein automatischer Neuversand.
- Der Netzmonitor sieht die vom eigenen Companion gelieferten Ereignisse, keine vollständige Netztopologie. Rohpakete und Statistikfelder hängen von Firmware und Funkverkehr ab. Fehlende Werte erscheinen als `—`. Die letzten 300 Ereignisse liegen im Arbeitsspeicher; die Aktivitätsgrafik zählt die darin beobachteten Funkpakete.
- Die API liefert weder Channel-Schlüssel noch Geräte-PINs aus. Nachrichten werden lokal unverschlüsselt in SQLite gespeichert.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Die Tests verwenden einen simulierten Companion und senden nichts ins Funknetz. Sie prüfen Persistenz, Duplikaterkennung, Empfangsrouting, UTF-8-Grenzen, ACKs, Fehlerfälle und API-Schutz.

Optionaler Browser-Test mit installiertem `/usr/bin/chromium` und laufender, verbundener Anwendung:

```sh
.venv/bin/python tests/browser_smoke.py http://127.0.0.1:8090
```

Er prüft die echte Verbindung lesend sowie Desktop/Mobilansicht, Entwürfe und simulierte Sendeaktionen. Screenshots liegen anschließend in `test-results/`. Alle Sendeanfragen dieses Tests werden abgefangen.

Protokollanbindung über [meshcore_py](https://github.com/meshcore-dev/meshcore_py); Firmware-Protokoll: [MeshCore Companion Protocol](https://github.com/meshcore-dev/MeshCore/blob/main/docs/companion_protocol.md).
