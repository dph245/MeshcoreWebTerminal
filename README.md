# MeshCore Web Terminal

Lokaler Webclient für einen MeshCore TCP-Companion auf **192.168.88.14:5000**.

- Vorhandene Channels und Chat-Kontakte vom Companion laden
- Hashtag-Channels im Companion hinzufügen und entfernen, einschließlich Rückleseprüfung
- Channel-Nachrichten und Direktnachrichten empfangen und senden
- Roomserver: Passwort-Anmeldung, Lese-/Schreibberechtigungen, eigene Verläufe mit Absenderkennung und bestätigtem Beitragsversand
- Nachrichtenverlauf in SQLite, auch nach Neustarts; ältere Nachrichten nachladen
- Empfangspfad unter eingehenden Nachrichten, mit Hop-Anzahl und verfügbaren Knoten-Hashes
- Scope des Absenders unter empfangenen Channel-Nachrichten; aus passenden Funkpaketen zugeordnet und mit dem Verlauf gespeichert. Fehlen passende Paketdaten, bleibt der Scope ausdrücklich nicht verfügbar.
- DM-Empfangsbestätigungen (ACK), Sendefehler und Verbindungsstatus
- Schalter „Zusätzliches ACK senden“ im Netzmonitor: Companion-Einstellung mit bestätigtem Rücklesen, verfügbar ab Protokollversion 7. Sie verbessert Bestätigungen für eingehende DMs bei bekannter direkter Rückroute; für ausgehende DMs zählt die Einstellung der Gegenstelle. Zusätzliche ACKs benötigen mehr Funkzeit. Die Einstellung wird erst beim Speichern verändert; Kontakt-, Telemetrie- und Advert-Einstellungen werden vom Gerät frisch gelesen und beibehalten.
- Live-Repeater-Zähler bei ausgehenden Channel-Nachrichten anhand zurückgehörter Weiterleitungen
- Netzmonitor mit Live-Funkereignissen, RSSI/SNR, Gerätestatistik und Ereignisfiltern
- Automatische Neuverbindung; responsive deutsche Oberfläche
- Standard-Scope im Companion und gespeicherte Scope-Auswahl pro Channel
- Scope des Absenders in Channel-Funkpaket-Details: Transportcode-Abgleich mit den bekannten Standard- und Channel-Scope-Namen. Passende Namen sind Kandidaten (16-Bit-Kollisionen möglich); unbekannte Namen und Pakete ohne Scope werden ausdrücklich gekennzeichnet. Der Scope-Name selbst wird nicht mitgesendet. Normale Channel-Empfangsereignisse ohne Funkpaket-Header enthalten keinen Scope.

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

Im Netzmonitor lässt sich unter **Pfad-Hash beim Senden** zwischen **1, 2 und 3 Byte pro Hop** wählen. Die Einstellung wird im Companion dauerhaft gespeichert und zurückgelesen; sie gilt für ausgehende Flood-Pakete. Bestehende direkte Routen behalten ihre eigene Hash-Länge. Längere Hashes reduzieren Kollisionen, benötigen aber mehr Platz pro Hop. Ohne entsprechenden Firmware-Support bleibt die Auswahl deaktiviert. Bei unbestätigter Änderung erfolgt eine Neuverbindung, bevor wieder gesendet werden kann.

Im Netzmonitor lässt sich der **Standard-Scope** des Companions lesen, ändern und mit „Ohne Scope“ löschen. Der Name wird mit `#` normalisiert und darf einschließlich `#` maximal 30 UTF-8-Bytes enthalten. Der Standard gilt für Flood-Verkehr, auch DMs ohne bekannte Route und Adverts; direkte Routen werden dadurch nicht verändert.

In jedem Channel stehen **Companion-Standard**, **Ohne Scope** und **Eigene Region** zur Auswahl. Mit „Speichern“ wird die Auswahl lokal in SQLite hinterlegt und von allen Browsern geteilt. Beim Senden wird der Channel-Scope gesetzt und anschließend zurückgesetzt, damit er nicht auf andere Channels oder DMs übergreift. Wenn die Rücksetzung nicht bestätigt wird, blockiert die Anwendung weitere Sendungen bis zur Neuverbindung. Empfangene Nachrichten werden durch diese Auswahl nicht gefiltert. Die Bedienfelder hängen von den Firmware-Fähigkeiten ab; unbekannte Standard-Scope-Werte werden als nicht verfügbar angezeigt.

Die Protokollrahmen folgen der [Companion-Firmware](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp). Der Adapter behandelt UTF-8-Padding und das Löschen des Standard-Scopes selbst, da `meshcore` 2.3.14 diese Fälle nicht korrekt kodiert.

- Über „Channels verwalten“ lassen sich Hashtag-Channels hinzufügen und vom Companion entfernen. Namen erhalten automatisch ein führendes `#`, bleiben ansonsten einschließlich Groß-/Kleinschreibung erhalten und dürfen maximal 31 UTF-8-Bytes umfassen. Der Schlüssel wird gemäß MeshCore aus den ersten 16 Bytes von SHA-256 des vollständigen Namens abgeleitet. Belegte Plätze werden nicht überschrieben; beim Entfernen werden Name und Schlüssel auf dem Companion geleert. Jede Änderung wird zurückgelesen. Private Channels und Kontakt-Import sind nicht Bestandteil dieser Verwaltung.
- Beim Entfernen oder erkannten Austausch eines Channels wird dessen lokaler Verlauf in SQLite archiviert und die lokale Scope-Auswahl entfernt. Archivierte Nachrichten werden nicht im Verlauf eines neu belegten Channel-Platzes angezeigt; eine Archivansicht gibt es noch nicht. Tests der Verwaltung verwenden ausschließlich simulierte Companion-Schreibvorgänge.
- Empfangen werden neue und noch im Companion gepufferte Nachrichten. Eine frühere Historie anderer Clients kann nicht nachträglich abgerufen werden.
- Empfangspfade werden zusammen mit neuen Nachrichten gespeichert. Bei Channels ergänzt die Bibliothek die Knotenfolge aus passenden entschlüsselten Funklogs, soweit diese während der Verbindung empfangen wurden. Eindeutige Kontakt-Präfixe werden zusätzlich als Namen angezeigt; mehrdeutige oder unbekannte bleiben als Hash sichtbar. DMs liefern häufig nur die Hop-Anzahl oder „Direct-Routing“ ohne Knotenfolge. Direct-Routing ist nicht gleichbedeutend mit null Hops. Fehlen Funklogs oder wurden Nachrichten vor dieser Erweiterung gespeichert, zeigt die Oberfläche die verfügbaren Angaben bzw. „nicht verfügbar“. Bestehende Datenbanken werden ohne Verlust des Verlaufs erweitert.
- „An Companion übergeben“ bestätigt die Übergabe an das Gerät. Nur ein DM-ACK führt zu „Zugestellt“. Channel-Nachrichten haben keine individuelle Empfangsbestätigung.
- Direktnachrichten werden bis zu dreimal per DIRECT gesendet, danach wird die gespeicherte Route zurückgesetzt und bis zu dreimal per FLOOD gesendet. Ohne gespeicherte Route beginnt der Versand direkt mit maximal drei FLOOD-Versuchen. Zwischen Versuchen wartet der Server die vom Companion vorgeschlagene ACK-Zeit plus 20 % (mindestens fünf Sekunden). Die Oberfläche zeigt Route und Versuch an; ein ACK aus einem beliebigen Versuch stoppt weitere Sendungen. Alle Versuche behalten Text und Zeitstempel und erhöhen die Protokoll-Versuchsnummer; im Verlauf bleibt eine Nachricht. Ohne ACK erscheint „Keine Zustellbestätigung“, auch später eintreffende ACKs werden weiterhin zugeordnet. Verbindungsabbruch, Sendefehler oder Serverneustart beenden die Wiederholungen mit „Sendeversuche abgebrochen“; sie werden nicht automatisch fortgesetzt. Ein fehlendes ACK beweist nicht, dass die Nachricht den Empfänger nicht erreicht hat.
- Bei ausgehenden Channel-Nachrichten wechselt die Anzeige nach passenden Funk-Echos zu „von 1 Repeater empfangen“, „von 2 Repeatern empfangen“ usw. Abgeglichen werden Channel-Name/-Hash, Sendezeit und vollständiger Nachrichtentext einschließlich Absender. Gezählt werden unterschiedliche letzte Hop-Hashes zurückgehörter Weiterleitungen, nicht die Länge des Pfads. Wiederholte Echos desselben Repeaters zählen einmal; die Anzahl bleibt nach Neustarts erhalten. Das ist eine beobachtete Mindestanzahl, keine vollständige Empfangsbestätigung: Nicht zurückgehörte Repeater fehlen, kurze Hashes können kollidieren. Ohne passende Echos bleibt „An Companion übergeben“ stehen. DMs verwenden weiterhin ACKs.
- Ausgehende Texte sind auf 160 UTF-8-Bytes begrenzt; bei Channels wird der Platz für den Absendernamen samt „: “ abgezogen, damit die Firmware den Text nicht abschneidet. Die Oberfläche zeigt das verbleibende Limit. Umlaute und Emojis können mehrere Bytes belegen. Bei unklarem Sendestatus erfolgt kein automatischer Neuversand.
- Der Netzmonitor sieht die vom eigenen Companion gelieferten Ereignisse, keine vollständige Netztopologie. Rohpakete und Statistikfelder hängen von Firmware und Funkverkehr ab. Fehlende Werte erscheinen als `—`. Die letzten 300 Ereignisse liegen im Arbeitsspeicher; die Aktivitätsgrafik zählt die darin beobachteten Funkpakete.
- Die API liefert weder Channel-Schlüssel noch Geräte-PINs aus. Nachrichten werden lokal unverschlüsselt in SQLite gespeichert.

## Roomserver

Im Companion gespeicherte Kontakte vom Typ Roomserver erscheinen in der eigenen Liste. Room öffnen, Passwort eingeben und **Anmelden / synchronisieren** wählen. Erst das passende `LOGIN_SUCCESS` bestätigt die Anmeldung; ohne Antwort erscheint ein Timeout. Leeres Passwort erlaubt Lesezugang, wenn der Server ihn unterstützt. Schreibrechte werden aus der Login-Antwort ausgewertet. Passwörter bleiben weder im Verlauf noch in SQLite oder im App-Zustand. Nach einer Neuverbindung oder einem Serverneustart ist eine neue Anmeldung nötig.

Empfangene Room-Beiträge (`SIGNED_PLAIN`) werden dem Room zugeordnet und mit dem vier Byte langen Absenderpräfix gespeichert; das ist keine vollständige digitale Signatur des Autors. Eindeutige bekannte Präfixe werden als Kontaktname angezeigt. Der Companion übernimmt Empfangs-ACKs, Synchronisationsstand und bei älteren Servern die vom Login angeforderten Keep-alives. Neuere Roomserver benötigen keine dauerhafte Keep-alive-Verbindung. Falls keine neuen Beiträge mehr eintreffen, erneut anmelden/synchronisieren.

Beiträge sind auf 150 UTF-8-Bytes begrenzt. Der Versand verwendet normale Textnachrichten mit ACK und derselben begrenzten DIRECT/FLOOD-Wiederholung wie DMs. „Vom Roomserver angenommen“ bestätigt den Serverempfang, nicht den Empfang aller Teilnehmer. Pro Room läuft jeweils ein Beitrag, damit neue Nachrichten die Wiederholungen älterer Beiträge nicht durch den Replay-Schutz des Servers ungültig machen. **Lokal abmelden** beendet die lokale Sitzung und ausstehende Sendeversuche; das Protokoll meldet den Nutzer nicht aus der Zugriffsliste des entfernten Servers ab. Weitere eingehende Beiträge bleiben deshalb möglich.

Protokollgrundlagen: [Companion-Firmware](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp), [Roomserver-Firmware](https://github.com/meshcore-dev/MeshCore/blob/main/examples/simple_room_server/MyMesh.cpp).

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python tests/browser_rooms.py
```

Die Tests verwenden einen simulierten Companion und senden nichts ins Funknetz. Sie prüfen Persistenz, Duplikaterkennung, Empfangsrouting, UTF-8-Grenzen, ACKs, Fehlerfälle und API-Schutz.

Optionaler Browser-Test mit installiertem `/usr/bin/chromium` und laufender, verbundener Anwendung:

```sh
.venv/bin/python tests/browser_smoke.py http://127.0.0.1:8090
```

Er prüft die echte Verbindung lesend sowie Desktop/Mobilansicht, Entwürfe und simulierte Sendeaktionen. Screenshots liegen anschließend in `test-results/`. Alle Sendeanfragen dieses Tests werden abgefangen.

Protokollanbindung über [meshcore_py](https://github.com/meshcore-dev/meshcore_py); Firmware-Protokoll: [MeshCore Companion Protocol](https://github.com/meshcore-dev/MeshCore/blob/main/docs/companion_protocol.md).
