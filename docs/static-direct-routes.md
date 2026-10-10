# Statische DIRECT-Routen

## Bedienung

Unter **Device → Kontakte verwalten → AUTO · Route / MANUAL · Route** wird pro
vollständigem Public Key eine Route konfiguriert. AUTO ist der Standard. MANUAL
speichert eine geordnete Liste von Repeater-Hashes in SQLite. Bekannte Repeater
können über die Kontaktvorschläge ausgewählt werden; alternativ sind Hex-Präfixe
möglich. Wiederholungen und Nullbytes sind zulässig, die Pfeile ändern die
Reihenfolge. Kollisionen unter bekannten Repeater-Schlüsseln ergeben eine Warnung;
unbekannte Repeater im Netz können weitere Kollisionen verursachen.

AUTO speichern löscht die manuelle Konfiguration. Der zuletzt im Companion
vorhandene Pfad bleibt zunächst erhalten und unterliegt wieder dem bisherigen
AUTO-Verhalten. Speichern selbst verursacht weder Funkverkehr noch eine
Aktivierung. Während laufender Nachrichtenversuche ist die Konfiguration des
betroffenen Ziels gesperrt. Die Konfiguration bleibt auch nach Neustart oder
Kontaktentfernung erhalten; sie wird nur für einen wieder gespeicherten Kontakt
mit exakt demselben vollständigen Schlüssel verwendet.

DIRECT erlaubt hier 1–63 Hops und insgesamt höchstens 64 Pfadbytes:

| Hash-Länge | Maximale Hops | Freigabe beim Versand |
| --- | --- | --- |
| 1 Byte | 63 | Setzen und identisches Rücklesen |
| 2 Bytes | 32 | zusätzlich Geräteantwort mit `path_hash_mode` 0–2 |
| 3 Bytes | 21 | zusätzlich Geräteantwort mit `path_hash_mode` 0–2 |
| 4 Bytes | nicht unterstützt | Firmware reserviert diesen Wert |

TRACE verwendet ein anderes Format (1/2/4/8 Bytes). Nur der Editor wird geteilt,
nicht Kodierung, Grenzwerte oder eine TRACE-Messung. Es wird kein TRACE ausgelöst.

## Versand und Bestätigung

AUTO bleibt unverändert: bei vorhandener DIRECT-Route höchstens drei
DIRECT-Versuche, danach höchstens drei FLOOD-Versuche; bei zunächst unbekanntem
Pfad direkt höchstens drei FLOOD-Versuche. Vor jedem FLOOD-Retry wird der Pfad
wie bisher zurückgesetzt.

MANUAL führt vor jeder adressierten Operation und vor jedem Nachrichten-Retry
unter dem gemeinsamen Bridge-Lock folgende Schritte aus:

1. Aktuelle Kontakte vom Companion lesen. Das Ziel muss existieren und sein
   6-Byte-Zielpräfix muss eindeutig sein (Text-/CLI-Kommandos adressieren nur
   dieses Präfix, obwohl die SQLite-Konfiguration den vollständigen Key nutzt).
2. `change_contact_path()` mit explizitem Hash-Modus auf einer Kontaktkopie
   ausführen. Diese API schreibt auch Kontaktmetadaten, daher das vorherige Lesen.
3. `OK` verlangen und `get_contact_by_key()` mit dem vollständigen Key ausführen.
4. Antworttyp, Key, Pfad, Hopzahl und Hash-Modus exakt vergleichen. Ein während
   der Aktivierung empfangenes PATH_UPDATE bricht sie konservativ ab.
5. Erst dann senden. Direkt vor dem Aufruf des Sendekommandos wird ein seit dem
   Rücklesen verarbeitetes PATH_UPDATE erneut ausgeschlossen. `MSG_SENT.type=0`
   ist erforderlich; ein anderer oder fehlender Modus beendet weitere Versuche.

Ein SDK-Cacheeintrag oder `OK` allein bestätigt keinen aktivierten Pfad. Scheitert
das Setzen/Rücklesen, wird nichts gesendet. MANUAL setzt bei ACK-Timeout keinen
Pfad zurück und fordert niemals FLOOD an. Es gibt höchstens drei DIRECT-Versuche.
CLI und Login behalten ihr bisheriges Verhalten mit einer Anfrage, ohne
zusätzliche automatische Wiederholung (CLI kann Seiteneffekte haben).

ACKs aller bisherigen Nachrichtenversuche bleiben zugeordnet. Auch ein später
ACK nach ausgeschöpften Versuchen oder Neustart kann die Nachricht als zugestellt
markieren. Neustarts führen zu keinen automatischen Wiederholungen. Ein während
der erneuten Routenaktivierung eingetroffener ACK verhindert den nächsten Retry.

Im Terminal und bei CLI ist die Routendiagnose aufklappbar. Sie unterscheidet
konfigurierte Route, noch nicht aktivierten Zustand, unmittelbar vor dem Versand
zurückgelesenen Pfad, veraltete Bestätigung nach PATH_UPDATE und Fehler.
Nachrichten speichern Modus, letzte bestätigte Pfadkonfiguration, Versandart und
Versuchsnummer; Zustellstatus unterscheidet ACK, Timeout und Abbruch.

## Verifizierte Operationsmatrix und Grenzen

Untersucht: installierte `meshcore==2.3.14`, insbesondere
`commands/contact.py`, `commands/messaging.py`, `reader.py`; außerdem öffentliche
Firmwarequellen am 2026-10-10:

- [Companion MyMesh.cpp](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp)
- [BaseChatMesh.cpp](https://github.com/meshcore-dev/MeshCore/blob/main/src/helpers/BaseChatMesh.cpp)
- [Packet.cpp](https://github.com/meshcore-dev/MeshCore/blob/main/src/Packet.cpp)

| Operation | Tatsächlicher Mechanismus | Abdeckung |
| --- | --- | --- |
| DM / Room-Beitrag | `SEND_TXT_MSG` → `sendMessage()` → Kontakt-`out_path` | Aktivierung vor jedem Versuch |
| Repeater-/Room-CLI | `SEND_TXT_MSG`, CLI_DATA → `sendCommandData()` → Kontakt-`out_path` | Aktivierung vor jedem Kommando |
| Repeater-/Room-Login | `SEND_LOGIN` → `sendLogin()` → Kontakt-`out_path`; Room-Login enthält `sync_since` | Aktivierung auch bei „Anmelden / synchronisieren“ |
| Lokales Logout | `CMD_LOGOUT` → `stopConnection()`; kein Funkpaket | Keine unnötige Aktivierung, funktioniert auch bei kaputter Route |
| Room-Keepalive / fortlaufende Synchronisation | `startConnection()` nach Login; `checkConnections()` sendet autonom über aktuellen Kontaktpfad | **Nicht statisch erzwingbar** |
| CLI-, Room-Antworten und automatische ACKs | Empfangs-/Antwortlogik der jeweiligen Firmware | **Rückweg nicht durch lokale Konfiguration festlegbar** |
| `get_msg()` | Leert nur die lokale Empfangswarteschlange | Keine adressierte Funkoperation |
| Channel, Advert/Discovery, manueller TRACE | Andere Routingmechanismen | Unverändert |

`BaseChatMesh::onContactPathRecv()` ersetzt den aktuellen Kontaktpfad bei neuen
Pfadinformationen. SQLite wird dadurch niemals verändert. PATH_UPDATE löst keine
zusätzliche Funkoperation aus; erst die nächste vom Terminal angeforderte
Operation aktiviert die gespeicherte Route erneut. Die globale Bridge-Sperre
verhindert, dass API-Aufrufe, Retry-Timer oder Remote-CLI zwischen Setzen,
Rücklesen und Senden eigene Kommandos einschieben. Eventcallbacks nehmen diese
Sperre nicht, damit Empfangsbestätigungen keinen Deadlock erzeugen.

**Companion-API-Grenze:** Es gibt weder eine atomare „Route setzen und senden“-
Operation noch einen persistenten Static-Route-Lock. Firmwareempfang kann selbst
zwischen Rücklesen und Verarbeitung des Sendekommandos einen anderen Pfad
lernen. Das Terminal kann diesen letzten Zeitraum nicht ausschließen und daher
auch nicht absolut garantieren, dass die Firmware niemals FLOOD sendet. Ein vom
Companion gemeldeter FLOOD-Versand wird als Fehler erkannt, kann aber nicht
zurückgenommen werden. `MSG_SENT` meldet nur DIRECT/FLOOD, nicht die tatsächlich
übertragenen Hopbytes. Die UI behauptet deshalb keine bestätigte Hopfolge des
Funkpakets.

Die autonome Room-Synchronisation läuft ohne Hostkommando und kann einen später
gelernten Pfad verwenden. Periodisches Zurücksetzen oder Reaktivieren in
PATH_UPDATE wäre kein zuverlässiger Lock und wird bewusst nicht als vollständige
Unterstützung implementiert. Auch eine bestehende Room-Verbindung kann während
eines Kontaktupdates weiter autonom senden. Vollständig statische Room-
Synchronisation oder atomar erzwungene Funkpfade wären ohne zusätzliche
Firmware-/Protokollfähigkeiten nicht erfüllbar. Es wurden keine Firmwareänderungen
oder ungeprüften Ersatzprotokolle eingebaut.

## Tests

`tests/test_routes.py` simuliert den Companion: Neustart, spät eintreffende ACKs,
PATH_UPDATE, nicht bestätigte/abweichende Aktivierung, Präfixkollisionen,
Mehrbyte-Grenzen einschließlich Nullbytes, konkurrierende Nachrichten/CLI und
Repeater-/Room-Operationen. Die echte installierte SDK-Kodierung wird mit
abgefangenem Transport für alle drei DIRECT-Formate geprüft.
`tests/browser_routes.py` prüft Editor, Warnungen, Reihenfolge, Wiederholungen,
Speichern/Löschen und Draft-Erhalt bei Zustandsereignissen mit abgefangenem HTTP.
Bestehende AUTO-, Room-, CLI-, TRACE- und übrige Tests bleiben Teil der Regression.
Keine Tests öffnen eine produktive Companion-Verbindung; es gab keine
Testsendungen oder TRACE-Messungen im Funknetz.

Validierung am 2026-10-10: **173 pytest-Tests bestanden**, einschließlich der
bestehenden AUTO-Regression. Alle neun Browser-Skripte bestanden (Routes, TRACE,
Navigation, Packets, Repeaters, Rooms, Contacts, Discovery, Smoke).
`git diff --check` ist sauber. Die Testsuite meldet lediglich die vorhandene
Starlette/httpx-Deprecation-Warnung. Chromium und FastAPI-TestClient wurden wegen
Sandbox-Beschränkungen außerhalb der Sandbox ausgeführt; die Tests blieben
vollständig simuliert. Kein Hardware-/Funknachweis wird daraus abgeleitet.

### Eingebettete ACKs in PATH-Paketen

`BaseChatMesh::onContactPathRecv()` verarbeitet `extra_type == PAYLOAD_TYPE_ACK`
über `processAck(extra)`. Der Companion erzeugt bei einem passenden Eintrag in
seiner erwarteten ACK-Tabelle zuerst `PUSH_CODE_PATH_UPDATED` (0x81), dann
`PUSH_CODE_SEND_CONFIRMED` (0x82). Das SDK liefert dafür die getrennten Ereignisse
`PATH_UPDATE` und `ACK`. Die Bridge berücksichtigt dieses ACK über denselben
ACK-Handler wie ein eigenständiges Funk-ACK; PATH_UPDATE allein ist keine
Zustellbestätigung. Die manuelle SQLite-Route bleibt unverändert.

Vier zusätzliche Regressionstests mit dem echten SDK-Frameparser bestanden:
AUTO/MANUAL, jeweils ACK während des Wartens oder bereits vor Rückgabe von
MSG_SENT. Sie prüfen Zustellung ohne weiteren Versuch oder reset_path und Erhalt
der manuellen Konfiguration. Dies simuliert Companion-Frames, keine
Funkentschlüsselung. Kann die Firmware ein sehr spätes ACK nicht mehr ihrer
begrenzten ACK-Tabelle zuordnen, erhält der Host auch keine entsprechende
Bestätigung; bloßer Empfang eines PATH-Pakets ersetzt diese nicht.
