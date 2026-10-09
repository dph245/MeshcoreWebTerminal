# MeshCore Web Terminal

Lokaler Webclient für einen MeshCore TCP-Companion auf **192.168.88.14:5000**.

- Vorhandene Channels und Chat-Kontakte vom Companion laden
- Kontaktverwaltung: Kontakte mit Name, Gerätetyp und vollständigem öffentlichen Schlüssel manuell hinzufügen oder vom Companion löschen; Änderungen werden zurückgelesen und bestätigt
- Hashtag-Channels im Companion hinzufügen und entfernen, einschließlich Rückleseprüfung
- Channel-Nachrichten und Direktnachrichten empfangen und senden
- Roomserver: Passwort-Anmeldung, Lese-/Schreibberechtigungen, eigene Verläufe mit Absenderkennung und bestätigtem Beitragsversand
- Nachrichtenverlauf in SQLite, auch nach Neustarts; ältere Nachrichten nachladen
- DM-Empfangsbestätigungen (ACK), Sendefehler und Verbindungsstatus
- Schalter „Zusätzliches ACK senden“ unter Device → Companion und Einstellungen: Companion-Einstellung mit bestätigtem Rücklesen, verfügbar ab Protokollversion 7. Sie verbessert Bestätigungen für eingehende DMs bei bekannter direkter Rückroute; für ausgehende DMs zählt die Einstellung der Gegenstelle. Zusätzliche ACKs benötigen mehr Funkzeit. Die Einstellung wird erst beim Speichern verändert; Kontakt-, Telemetrie- und Advert-Einstellungen werden vom Gerät frisch gelesen und beibehalten.
- Live-Repeater-Zähler bei ausgehenden Channel-Nachrichten anhand zurückgehörter Weiterleitungen
- Netzmonitor mit Live-Funkpaketen, RSSI/SNR, Pakettypfilter und Details auf Abruf
- DISCOVER aussenden, Antworten und ADVERTs dauerhaft sammeln und gefundene Geräte gezielt als Kontakt im Companion speichern
- Automatische Neuverbindung; responsive deutsche Oberfläche
- Standard-Scope im Companion und gespeicherte Scope-Auswahl pro Channel
- Scope des Absenders in Channel-Funkpaket-Details: Transportcode-Abgleich mit den bekannten Standard- und Channel-Scope-Namen. Passende Namen sind Kandidaten (16-Bit-Kollisionen möglich); unbekannte Namen und Pakete ohne Scope werden ausdrücklich gekennzeichnet. Der Scope-Name selbst wird nicht mitgesendet. Normale Channel-Empfangsereignisse ohne Funkpaket-Header enthalten keinen Scope.

## Arbeitsbereiche

- **Terminal** (Startansicht): Channels, Direktnachrichten und Roomserver. Gespräch und Entwurf bleiben beim Tabwechsel erhalten; Nachrichten außerhalb des Terminals bleiben ungelesen.
- **Netzmonitor**: passiver Live-Paketstrom mit Typ, verfügbarem Absender/Inhalt, Zeit und RSSI/SNR. Details enthalten Routing, Scope und Rohdaten. Keine Einstellungen oder Statistikgrafiken.
- **Device**: Geräteauswahl, Remote-CLI und Antworten. Kontakt-/Channelverwaltung sowie Companion-Einstellungen werden nur auf Anfrage aufgeklappt. Room-Chat und CLI verwenden dieselbe Geräteanmeldung; vor dem Wechsel muss die bisherige Sitzung abgemeldet werden. Eine lokale Companion-CLI über TCP wird vom vorhandenen Adapter nicht angeboten.
- **Mesh**: DISCOVER und dauerhaft gespeicherte Antworten/ADVERTs. Das SDK sendet eine ungerichtete Anfrage, kein gezieltes DISCOVER an einen Schlüssel. TRACE ist noch nicht implementiert.

Die Anwendung bleibt bei FastAPI, SQLite, SSE und Vanilla-JavaScript, ohne Buildschritt und ausschließlich mit TCP. Der Tabzustand ist von der Gesprächsauswahl getrennt. Weitere Mesh-Operationen können im eigenen Panel ergänzt werden, ohne Terminal oder Monitor zu verändern.

Für den Netzmonitor wurde die lokale MeshCoreOnAir-LiveView (`static/app.js`, `render`) geprüft: kompakte Empfangszeilen, neueste zuerst, Filter, Pause und Details auf Abruf. Ihre MQTT-/Observer-Gruppierung und ihr Decoder passen nicht zum TCP-SDK-Datenmodell. Deshalb wird die vorhandene Paketdarstellung in `static/packets.js` weiterverwendet und verdichtet; es werden keine Observer-Hashes oder Absender aus Pfad-Hashes erfunden.

## Kontakte verwalten

Unter **Device → Kontakte verwalten** öffnest du die Kontaktliste. Zum Hinzufügen brauchst du einen Namen (maximal 31 UTF-8-Bytes), den Gerätetyp (Chat, Repeater, Roomserver oder Sensor) und den vollständigen öffentlichen Schlüssel (64 Hex-Zeichen). Vorhandene Schlüssel werden abgewiesen, damit ein bestehender Kontakt nicht versehentlich überschrieben wird.

Die durchsuchbare Liste zeigt alle gespeicherten Gerätetypen und bietet **Löschen** an. Beide Aktionen benötigen eine Verbindung zum Companion und prüfen das Ergebnis durch erneutes Auslesen. Ein laufender Nachrichtenversand oder eine Roomserver-Anmeldung muss vor dem Löschen abgeschlossen sein. Nachrichtenverläufe und Fundliste bleiben erhalten; nach erneutem Hinzufügen desselben Schlüssels ist der Verlauf wieder erreichbar. Die Auto-Add-Einstellung des Companions kann Kontakte bei späteren ADVERTs erneut hinzufügen.

## Geräte entdecken

Im Tab **Mesh** sendet **DISCOVER aussenden** eine Anfrage für Chat-Geräte, Repeater, Roomserver und Sensoren. Antworten hängen von Gerätetyp, Firmware und Erreichbarkeit ab. Die Anfrage fordert vollständige Schlüssel an ([MeshCore-Protokoll](https://github.com/meshcore-dev/MeshCore/blob/main/docs/payloads.md#control-data)).

**Entdeckte Geräte** sammelt DISCOVER-Antworten und ADVERTs automatisch in SQLite, unabhängig vom begrenzten Ereignisprotokoll. Pro Geräteschlüssel bleiben Quellen, erster/letzter Empfang, Kontaktdaten und die letzte Beobachtung je Ereignistyp erhalten. Bei DISCOVER-Antworten zeigt die Liste beide SNR-Werte in dB: den Empfang der Antwort beim eigenen Companion und den Empfang der Anfrage beim antwortenden Gerät. Fehlende Werte werden als nicht verfügbar angezeigt. Die Liste lässt sich nach Name oder Schlüssel durchsuchen und bleibt auch offline verfügbar.

**Als Kontakt speichern** übernimmt ein Gerät in den Companion und liest das Kontaktbuch zur Bestätigung zurück. Bereits gespeicherte Kontakte werden nicht überschrieben. Ohne Advert-Namen wird zunächst der Schlüsselanfang als Name verwendet. Reine Schlüsselpräfixe oder Meldungen ohne Gerätetyp bleiben in der Fundliste, bis vollständige Daten vorliegen; Präfixe werden nicht automatisch einem vollständigen Schlüssel zugeordnet. Die Auto-Add-Einstellung des Companions bleibt unverändert. Repeater erscheinen in der Fundliste als gespeichert, Chat-Geräte und Roomserver zusätzlich in der jeweiligen Seitenleiste.

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

Unter **Device → Companion und Einstellungen** lässt sich unter **Pfad-Hash beim Senden** zwischen **1, 2 und 3 Byte pro Hop** wählen. Die Einstellung wird im Companion dauerhaft gespeichert und zurückgelesen; sie gilt für ausgehende Flood-Pakete. Bestehende direkte Routen behalten ihre eigene Hash-Länge. Längere Hashes reduzieren Kollisionen, benötigen aber mehr Platz pro Hop. Ohne entsprechenden Firmware-Support bleibt die Auswahl deaktiviert. Bei unbestätigter Änderung erfolgt eine Neuverbindung, bevor wieder gesendet werden kann.

Unter **Device → Companion und Einstellungen** lässt sich der **Standard-Scope** des Companions lesen, ändern und mit „Ohne Scope“ löschen. Der Name wird mit `#` normalisiert und darf einschließlich `#` maximal 30 UTF-8-Bytes enthalten. Der Standard gilt für Flood-Verkehr, auch DMs ohne bekannte Route und Adverts; direkte Routen werden dadurch nicht verändert.

Im **Terminal direkt im Kopf des geöffneten Channels** stehen für dessen Scope **Companion-Standard**, **Ohne Scope** und **Eigene Region** zur Auswahl. Mit „Speichern“ wird die Auswahl lokal in SQLite hinterlegt und von allen Browsern geteilt. Beim Senden wird der Channel-Scope gesetzt und anschließend zurückgesetzt, damit er nicht auf andere Channels oder DMs übergreift. Wenn die Rücksetzung nicht bestätigt wird, blockiert die Anwendung weitere Sendungen bis zur Neuverbindung. Empfangene Nachrichten werden durch diese Auswahl nicht gefiltert. Die Bedienfelder hängen von den Firmware-Fähigkeiten ab; unbekannte Standard-Scope-Werte werden als nicht verfügbar angezeigt.

Die Protokollrahmen folgen der [Companion-Firmware](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp). Der Adapter behandelt UTF-8-Padding und das Löschen des Standard-Scopes selbst, da `meshcore` 2.3.14 diese Fälle nicht korrekt kodiert.

- Über „Channels verwalten“ lassen sich Hashtag-Channels hinzufügen und vom Companion entfernen. Namen erhalten automatisch ein führendes `#`, bleiben ansonsten einschließlich Groß-/Kleinschreibung erhalten und dürfen maximal 31 UTF-8-Bytes umfassen. Der Schlüssel wird gemäß MeshCore aus den ersten 16 Bytes von SHA-256 des vollständigen Namens abgeleitet. Belegte Plätze werden nicht überschrieben; beim Entfernen werden Name und Schlüssel auf dem Companion geleert. Jede Änderung wird zurückgelesen. Private Channels und Kontakt-Import sind nicht Bestandteil dieser Verwaltung.
- Beim Entfernen oder erkannten Austausch eines Channels wird dessen lokaler Verlauf in SQLite archiviert und die lokale Scope-Auswahl entfernt. Archivierte Nachrichten werden nicht im Verlauf eines neu belegten Channel-Platzes angezeigt; eine Archivansicht gibt es noch nicht. Tests der Verwaltung verwenden ausschließlich simulierte Companion-Schreibvorgänge.
- Empfangen werden neue und noch im Companion gepufferte Nachrichten. Eine frühere Historie anderer Clients kann nicht nachträglich abgerufen werden.
- Empfangspfade werden zusammen mit neuen Nachrichten gespeichert, aber nicht im Terminal angezeigt. Funkpaketdetails stehen im Netzmonitor bereit. Bei Channels ergänzt die Bibliothek die Knotenfolge aus passenden entschlüsselten Funklogs, soweit diese während der Verbindung empfangen wurden. Eindeutige Kontakt-Präfixe werden zusätzlich als Namen angezeigt; mehrdeutige oder unbekannte bleiben als Hash sichtbar. DMs liefern häufig nur die Hop-Anzahl oder „Direct-Routing“ ohne Knotenfolge. Direct-Routing ist nicht gleichbedeutend mit null Hops. Fehlen Funklogs oder wurden Nachrichten vor dieser Erweiterung gespeichert, bleiben diese Angaben unvollständig. Bestehende Datenbanken werden ohne Verlust des Verlaufs erweitert.
- „An Companion übergeben“ bestätigt die Übergabe an das Gerät. Nur ein DM-ACK führt zu „Zugestellt“. Channel-Nachrichten haben keine individuelle Empfangsbestätigung.
- Direktnachrichten werden bis zu dreimal per DIRECT gesendet, danach wird die gespeicherte Route zurückgesetzt und bis zu dreimal per FLOOD gesendet. Ohne gespeicherte Route beginnt der Versand direkt mit maximal drei FLOOD-Versuchen. Zwischen Versuchen wartet der Server die vom Companion vorgeschlagene ACK-Zeit plus 20 % (mindestens fünf Sekunden). Die Oberfläche zeigt Route und Versuch an; ein ACK aus einem beliebigen Versuch stoppt weitere Sendungen. Alle Versuche behalten Text und Zeitstempel und erhöhen die Protokoll-Versuchsnummer; im Verlauf bleibt eine Nachricht. Ohne ACK erscheint „Keine Zustellbestätigung“, auch später eintreffende ACKs werden weiterhin zugeordnet. Verbindungsabbruch, Sendefehler oder Serverneustart beenden die Wiederholungen mit „Sendeversuche abgebrochen“; sie werden nicht automatisch fortgesetzt. Ein fehlendes ACK beweist nicht, dass die Nachricht den Empfänger nicht erreicht hat.
- Bei ausgehenden Channel-Nachrichten wechselt die Anzeige nach passenden Funk-Echos zu „von 1 Repeater empfangen“, „von 2 Repeatern empfangen“ usw. Abgeglichen werden Channel-Name/-Hash, Sendezeit und vollständiger Nachrichtentext einschließlich Absender. Gezählt werden unterschiedliche letzte Hop-Hashes zurückgehörter Weiterleitungen, nicht die Länge des Pfads. Wiederholte Echos desselben Repeaters zählen einmal; die Anzahl bleibt nach Neustarts erhalten. Das ist eine beobachtete Mindestanzahl, keine vollständige Empfangsbestätigung: Nicht zurückgehörte Repeater fehlen, kurze Hashes können kollidieren. Ohne passende Echos bleibt „An Companion übergeben“ stehen. DMs verwenden weiterhin ACKs.
- Ausgehende Texte sind auf 160 UTF-8-Bytes begrenzt; bei Channels wird der Platz für den Absendernamen samt „: “ abgezogen, damit die Firmware den Text nicht abschneidet. Die Oberfläche zeigt das verbleibende Limit. Umlaute und Emojis können mehrere Bytes belegen. Bei unklarem Sendestatus erfolgt kein automatischer Neuversand.
- Der Netzmonitor sieht die vom eigenen Companion gelieferten Ereignisse, keine vollständige Netztopologie. Rohpakete und Statistikfelder hängen von Firmware und Funkverkehr ab. Fehlende Werte erscheinen als `—`. Die letzten 300 Ereignisse liegen im Arbeitsspeicher; die Live-Liste zeigt daraus ausschließlich RX_LOG_DATA, neueste zuerst. Pause friert die Ansicht ein, während der Empfang weiterläuft. RAW_DATA und interne Statusereignisse sind keine zusätzlichen Funkempfänge und erscheinen dort nicht.
- Die API liefert weder Channel-Schlüssel noch Geräte-PINs aus. Nachrichten werden lokal unverschlüsselt in SQLite gespeichert.

## Roomserver

Im Companion gespeicherte Kontakte vom Typ Roomserver erscheinen in der eigenen Liste. Room öffnen, Passwort eingeben und **Anmelden / synchronisieren** wählen. Erst das passende `LOGIN_SUCCESS` bestätigt die Anmeldung; ohne Antwort erscheint ein Timeout. Leeres Passwort erlaubt Lesezugang, wenn der Server ihn unterstützt. Schreibrechte werden aus der Login-Antwort ausgewertet. Passwörter bleiben weder im Verlauf noch in SQLite oder im App-Zustand. Nach einer Neuverbindung oder einem Serverneustart ist eine neue Anmeldung nötig.

Empfangene Room-Beiträge (`SIGNED_PLAIN`) werden dem Room zugeordnet und mit dem vier Byte langen Absenderpräfix gespeichert; das ist keine vollständige digitale Signatur des Autors. Eindeutige bekannte Präfixe werden als Kontaktname angezeigt. Der Companion übernimmt Empfangs-ACKs, Synchronisationsstand und bei älteren Servern die vom Login angeforderten Keep-alives. Neuere Roomserver benötigen keine dauerhafte Keep-alive-Verbindung. Falls keine neuen Beiträge mehr eintreffen, erneut anmelden/synchronisieren.

Beiträge sind auf 150 UTF-8-Bytes begrenzt. Der Versand verwendet normale Textnachrichten mit ACK und derselben begrenzten DIRECT/FLOOD-Wiederholung wie DMs. „Vom Roomserver angenommen“ bestätigt den Serverempfang, nicht den Empfang aller Teilnehmer. Pro Room läuft jeweils ein Beitrag, damit neue Nachrichten die Wiederholungen älterer Beiträge nicht durch den Replay-Schutz des Servers ungültig machen. **Lokal abmelden** beendet die lokale Sitzung und ausstehende Sendeversuche; das Protokoll meldet den Nutzer nicht aus der Zugriffsliste des entfernten Servers ab. Weitere eingehende Beiträge bleiben deshalb möglich.

Protokollgrundlagen: [Companion-Firmware](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp), [Roomserver-Firmware](https://github.com/meshcore-dev/MeshCore/blob/main/examples/simple_room_server/MyMesh.cpp).

## Nachrichtenlisten

Kanäle, Direktnachrichten und Roomserver zeigen bei ungelesenen eingehenden
Nachrichten einen roten Punkt. Beim Lesen am Ende des geöffneten Verlaufs
verschwindet er; Hintergrund-Tabs markieren Nachrichten nicht als gelesen.
Die Sortierung in der Seitenleiste bietet **ABC** und **Zuletzt benutzt**
(Öffnen oder letzte Nachricht). Lesestand, letzte Öffnung und Sortierauswahl
werden pro Browser gespeichert und bleiben beim Neuladen erhalten.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python tests/browser_rooms.py
.venv/bin/python tests/browser_discovery.py
.venv/bin/python tests/browser_contacts.py
.venv/bin/python tests/browser_navigation.py
```

Die Tests verwenden einen simulierten Companion und senden nichts ins Funknetz. Sie prüfen Persistenz, Duplikaterkennung, Empfangsrouting, UTF-8-Grenzen, ACKs, Fehlerfälle und API-Schutz.

Browser-Test mit installiertem `/usr/bin/chromium` (vollständig simuliert, kein laufender Server erforderlich):

```sh
.venv/bin/python tests/browser_smoke.py http://127.0.0.1:8090
```

Er prüft Desktop/Mobilansicht, Channelverwaltung, Scope-Einstellungen und simulierte Sendeaktionen. Screenshots liegen anschließend in `test-results/`. Alle Sendeanfragen dieses Tests werden abgefangen.

Protokollanbindung über [meshcore_py](https://github.com/meshcore-dev/meshcore_py); Firmware-Protokoll: [MeshCore Companion Protocol](https://github.com/meshcore-dev/MeshCore/blob/main/docs/companion_protocol.md).

### Geräte-CLI

Unter **Device → Geräte-CLI** einen im Companion gespeicherten Repeater oder Roomserver
wählen, mit dessen Passwort anmelden und die Anmeldebestätigung abwarten.
Danach einzelne CLI-Kommandos wie `get name` senden. Die Anzeige bestätigt zunächst
nur die Übergabe an den Companion; Antworten erscheinen darunter. Es gibt keine
automatische Wiederholung. Die verfügbaren Kommandos und Berechtigungen hängen
von der Geräte-Firmware ab; siehe die
[MeshCore CLI-Referenz](https://github.com/meshcore-dev/MeshCore/blob/main/docs/cli_commands.md).

Die aufklappbare **Kommandoliste** enthält eine lokal hinterlegte, durchsuchbare
Referenz mit Syntax und deutschen Kurzbeschreibungen (Stand 09.10.2026,
[MeshCore-Dokumentation](https://docs.meshcore.io/cli_commands/)). Sie berücksichtigt
den ausgewählten Gerätetyp und kennzeichnet rein serielle Befehle. Ohne ausgewähltes
Gerät werden beide Typen angezeigt. Die Liste führt keine Kommandos aus.

Passwörter und gesendete Kommandos werden nicht im Anwendungsverlauf gespeichert.
Die letzten 50 CLI-Antworten je Gerät bleiben im Serverspeicher und werden beim
Abmelden gelöscht. Alle verbundenen Webclients teilen diese Sitzungen. Nach einer
unterbrochenen Companion-Verbindung erneut anmelden; auch bei einer abgelaufenen
Gerätesitzung kann eine erneute Anmeldung nötig sein.
