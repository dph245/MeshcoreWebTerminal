"""Local MeshCore TCP bridge, persistent chat history and live event stream."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from meshcore import EventType, MeshCore
from meshcore.tcp_cx import TCPConnection
from meshcore.packets import CommandType
from pydantic import BaseModel, Field, SecretStr


def public(value):
    """JSON-safe payload without device PINs or channel secrets."""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): public(v) for k, v in value.items()
                if not any(s in str(k).lower() for s in ("secret", "private", "pin", "password"))}
    if isinstance(value, (list, tuple)):
        return [public(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def receive_path(payload):
    """Preserve only receive metadata; contact out_path is never an RX route."""
    count = payload.get("path_len")
    if count == 255:
        # Direct routing does not mean a zero-hop radio link.
        return {"routing": "direct", "hops": None, "path": None}
    if not isinstance(count, int) or not 0 <= count <= 63:
        return {"routing": "unknown", "hops": None, "path": None}
    result = {"routing": "flood", "hops": count, "path": [] if count == 0 else None}
    raw = payload.get("path")
    mode = payload.get("path_hash_mode")
    if isinstance(raw, str) and isinstance(mode, int) and 0 <= mode <= 2:
        width = (mode + 1) * 2
        if len(raw) == count * width and all(c in "0123456789abcdefABCDEF" for c in raw):
            result["path"] = [raw[i:i + width].lower() for i in range(0, len(raw), width)]
    return result


def channel_echo_key(channel_name, channel_hash, timestamp, message):
    return hashlib.sha256(json.dumps([channel_name, channel_hash, timestamp, message],
                                    ensure_ascii=False).encode()).hexdigest()


def channel_packet_key(payload):
    if payload.get("payload_type") != 5:
        return None
    if not isinstance(payload.get("message"), str) or not isinstance(payload.get("sender_timestamp"), int):
        return None
    if not isinstance(payload.get("chan_name"), str) or not isinstance(payload.get("chan_hash"), str):
        return None
    return channel_echo_key(payload["chan_name"], payload["chan_hash"], payload["sender_timestamp"], payload["message"])


def packet_scope(payload, known_scopes):
    """Match RX transport code against known names, never assume our TX scope.

    MeshCore TransportKeyStore.cpp: HMAC-SHA256(scope key, type + payload),
    truncated to a little-endian uint16, with 0/65535 reserved. A 16-bit match
    is only a candidate, not proof of the sender's configuration.
    """
    route = payload.get("route_type")
    if route in (1, 2):
        return {"status": "unscoped"}
    if route not in (0, 3):
        return {"status": "unknown"}
    raw = payload.get("transport_code")
    if not isinstance(raw, str) or len(raw) != 8 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        return {"status": "unknown"}
    code = int.from_bytes(bytes.fromhex(raw)[:2], "little")
    result = {"status": "scoped", "code": f"0x{code:04X}", "candidates": []}
    if code in (0, 65535):
        result["status"] = "unknown"  # Reserved codes cannot identify a named scope.
        return result
    packet = payload.get("pkt_payload")
    kind = payload.get("payload_type")
    if not isinstance(packet, str) or not packet or len(packet) % 2 or any(c not in "0123456789abcdefABCDEF" for c in packet):
        return result
    if not isinstance(kind, int) or not 0 <= kind <= 15:
        return result
    data = bytes([kind]) + bytes.fromhex(packet)
    for name in sorted({s for s in known_scopes if isinstance(s, str) and s.startswith("#")}):
        key = hashlib.sha256(name.encode("utf-8")).digest()[:16]
        expected = int.from_bytes(hmac.new(key, data, hashlib.sha256).digest()[:2], "little")
        expected = max(1, min(65534, expected))
        if expected == code:
            result["candidates"].append(name)
    return result


def repeater_echo(payload):
    """Identify a decrypted channel echo and its last (audible) forwarding hop."""
    if payload.get("payload_type") != 5 or payload.get("route_type") not in (0, 1):
        return None
    count, size, path = payload.get("path_len"), payload.get("path_hash_size"), payload.get("path")
    if not isinstance(count, int) or not 1 <= count <= 63 or size not in (1, 2, 3):
        return None
    if not isinstance(path, str) or len(path) != count * size * 2 or any(c not in "0123456789abcdefABCDEF" for c in path):
        return None
    if not isinstance(payload.get("message"), str) or not isinstance(payload.get("sender_timestamp"), int):
        return None
    if not isinstance(payload.get("chan_name"), str) or not isinstance(payload.get("chan_hash"), str):
        return None
    key = channel_echo_key(payload["chan_name"], payload["chan_hash"], payload["sender_timestamp"], payload["message"])
    return key, path[-size * 2:].lower()


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, kind TEXT, target TEXT, direction TEXT,
                text TEXT, timestamp REAL, status TEXT, ack TEXT, fingerprint TEXT UNIQUE
            );
            CREATE INDEX IF NOT EXISTS conversation ON messages(kind, target, id);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        """)
        # Additive migration: existing chat history stays intact.
        if "reception" not in {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}:
            with self.db:
                self.db.execute("ALTER TABLE messages ADD COLUMN reception TEXT")
        with self.db:
            if "echo_key" not in {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}:
                self.db.execute("ALTER TABLE messages ADD COLUMN echo_key TEXT")
            self.db.execute("CREATE INDEX IF NOT EXISTS message_echo ON messages(echo_key)")
            self.db.execute("CREATE TABLE IF NOT EXISTS repeater_receipts (message_id INTEGER, hop TEXT, PRIMARY KEY(message_id, hop))")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
            for name, definition in (("tx_route", "TEXT"), ("tx_attempt", "INTEGER"), ("sender_key", "TEXT")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
            self.db.execute("CREATE TABLE IF NOT EXISTS message_acks (message_id INTEGER, code TEXT, PRIMARY KEY(message_id, code))")
            self.db.execute("CREATE INDEX IF NOT EXISTS message_ack_code ON message_acks(code)")
            # A process restart must not silently restart radio transmissions.
            self.db.execute("UPDATE messages SET status='interrupted' WHERE status='sending'")

    def save(self, kind, target, direction, text, timestamp, status, ack=None, reception=None, echo_key=None, sender_key=None):
        fingerprint = None
        if direction == "in":
            identity = [kind, target, text, timestamp]
            if kind == "room":
                identity.append(sender_key)
            fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO messages(kind,target,direction,text,timestamp,status,ack,fingerprint,reception,echo_key,sender_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (kind, target, direction, text, timestamp, status, ack, fingerprint,
                 json.dumps(reception) if reception is not None else None, echo_key, sender_key))
        return cursor.lastrowid if cursor.rowcount else None

    def history(self, kind, target, before=None):
        rows = self.db.execute(
            "SELECT *, (SELECT count(*) FROM repeater_receipts WHERE message_id=messages.id) AS repeater_count FROM messages WHERE kind=? AND target=? AND id<? ORDER BY id DESC LIMIT 100",
            (kind, target, before or 9223372036854775807)).fetchall()
        result = [dict(row) for row in reversed(rows)]
        for message in result:
            message["reception"] = json.loads(message["reception"]) if message["reception"] else None
        return result

    def record_repeater(self, key, hop):
        rows = self.db.execute("SELECT id FROM messages WHERE echo_key=? AND direction='out' AND kind='channel' LIMIT 2", (key,)).fetchall()
        if len(rows) != 1:
            return False  # Ambiguous identical transmissions cannot be attributed safely.
        mid = rows[0][0]
        known = [row[0] for row in self.db.execute("SELECT hop FROM repeater_receipts WHERE message_id=?", (mid,))]
        if any(hop.startswith(old) or old.startswith(hop) for old in known):
            return False  # Repeated echoes / overlapping short hashes count once.
        with self.db:
            self.db.execute("INSERT INTO repeater_receipts VALUES(?,?)", (mid, hop))
        return True

    def record_received_scope(self, key, scope):
        rows = self.db.execute(
            "SELECT id,reception FROM messages WHERE echo_key=? AND direction='in' AND kind='channel' LIMIT 2",
            (key,)).fetchall()
        if len(rows) != 1 or scope.get("status") == "unknown":
            return False
        reception = json.loads(rows[0]["reception"]) if rows[0]["reception"] else {}
        if reception.get("scope"):
            return False  # Keep the first observed scope, including across later relays.
        reception["scope"] = scope
        with self.db:
            self.db.execute("UPDATE messages SET reception=? WHERE id=?", (json.dumps(reception), rows[0]["id"]))
        return True

    def acknowledge(self, code):
        with self.db:
            self.db.execute("""UPDATE messages SET status='delivered' WHERE direction='out'
                AND (ack=? OR id IN (SELECT message_id FROM message_acks WHERE code=?))""", (code, code))

    def record_attempt(self, mid, code, route, attempt):
        with self.db:
            if code:
                self.db.execute("INSERT OR IGNORE INTO message_acks VALUES(?,?)", (mid, code))
            self.db.execute("""UPDATE messages SET ack=?, tx_route=?, tx_attempt=?,
                status=CASE WHEN status='delivered' THEN status ELSE 'sending' END WHERE id=?""",
                (code, route, attempt, mid))

    def delivered(self, mid):
        return self.db.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()[0] == "delivered"

    def finish_send(self, mid, status):
        with self.db:
            self.db.execute("UPDATE messages SET status=? WHERE id=? AND status!='delivered'", (status, mid))

    def archive_channel(self, index):
        # Slot numbers are reused by the radio; old messages must not become
        # the history of an unrelated channel occupying the same slot.
        with self.db:
            self.db.execute("UPDATE messages SET target=?, fingerprint=NULL WHERE kind='channel' AND target=?",
                            (f"archived:{index}:{uuid.uuid4().hex}", str(index)))

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(value)))

    def get_meta(self, key, default):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


class Bridge:
    def __init__(self, store, host, port):
        self.store, self.host, self.port = store, host, port
        self.radio = None
        self.lock = asyncio.Lock()
        self.desired = False
        self.ready = False
        self.status = "offline"
        self.error = None
        self.channels = store.get_meta("channels", [])
        self.contacts = store.get_meta("contacts", {})
        self.discovered = store.get_meta("discovered", {})
        self.info = {}
        self.device = {}
        self.stats = {}
        self.default_scope = None
        self.scope_supported = False
        self.channel_scopes = store.get_meta("channel_scopes", {})
        self.channel_hashes = {}
        self.channel_names = {}
        self.recent_echoes = deque(maxlen=300)
        self.recent_scopes = deque(maxlen=300)
        self.events = deque(maxlen=300)
        self.acks = deque(maxlen=200)
        self.listeners = set()
        self.task = None
        self.pending_dms = {}
        self.dm_wakeups = {}
        self.repeaters = {}
        self.repeater_login_tasks = {}
        self.rooms = {}
        self.room_login_task = None
        self.room_sends = {}
        self.room_timestamps = {}

    def snapshot(self):
        return public(dict(status=self.status, error=self.error, host=self.host, port=self.port,
                           channels=self.channels, contacts=self.contacts, discovered=self.discovered, info=self.info,
                           device=self.device, stats=self.stats, events=list(self.events),
                           repeaters=self.repeaters,
                           rooms={key: {**room, "pending_send": key in self.room_sends} for key, room in self.rooms.items()},
                           scopes={"default": self.default_scope, "supported": self.scope_supported,
                                   "channels": self.channel_scopes}))

    def emit(self, event, payload):
        for queue in tuple(self.listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait({"event": event, "data": public(payload)})

    def changed(self):
        self.emit("state", self.snapshot())

    def log(self, kind, payload):
        event = {"time": time.time(), "type": kind, "payload": public(payload)}
        if kind == "RX_LOG_DATA" and event["payload"].get("payload_type") == 5:
            event["payload"]["received_scope"] = packet_scope(
                event["payload"], [self.default_scope, *self.channel_scopes.values()])
            key = channel_packet_key(event["payload"])
            if key:
                scope = event["payload"]["received_scope"]
                self.recent_scopes.append((key, scope))
                if self.store.record_received_scope(key, scope):
                    self.emit("message", {"received_scope": True})
        self.events.append(event)
        self.emit("radio", event)

    async def on_event(self, event):
        p = public(event.payload or {})
        name = event.type.name
        if name in ("DISCOVER_RESPONSE", "ADVERTISEMENT", "NEW_CONTACT") or (name == "RX_LOG_DATA" and p.get("payload_type") == 4):
            self.remember_discovery(name, p)
        if name == "SELF_INFO":
            self.info = p
        elif name == "DEVICE_INFO":
            self.device = p
        elif name.startswith("STATS_"):
            self.stats[name] = p
            self.changed()
        elif name == "CONTACT_MSG_RECV" and p.get("txt_type") == 1:
            prefix = p.get("pubkey_prefix")
            matches = [key for key in self.repeaters if prefix and key.startswith(prefix)]
            if len(matches) == 1:
                session = self.repeaters[matches[0]]
                session["replies"] = (session["replies"] + [{"time": time.time(), "text": p.get("text", "")}])[-50:]
                self.changed()
            # CLI replies may contain configuration secrets; keep them out of persistent chat/event logs.
        elif name in ("CONTACT_MSG_RECV", "CHANNEL_MSG_RECV"):
            kind = "channel" if name == "CHANNEL_MSG_RECV" else "dm"
            target = str(p["channel_idx"]) if kind == "channel" else p["pubkey_prefix"]
            sender_key = None
            if kind == "dm":
                room = any(k.startswith(target) and c.get("type") == 3 for k, c in self.contacts.items())
                if room or p.get("txt_type") == 2:
                    kind = "room"
                    sender_key = p.get("signature") if p.get("txt_type") == 2 else target
            key = None
            if kind == "channel" and target in self.channel_hashes and isinstance(p.get("sender_timestamp"), int):
                channel_name = self.channel_names.get(target)
                if channel_name is not None:
                    key = channel_echo_key(channel_name, self.channel_hashes[target], p["sender_timestamp"], p.get("text", ""))
            # DMs use the protocol's 6-byte prefix, including for unknown senders.
            mid = self.store.save(kind, target, "in", p.get("text", ""),
                                  p.get("sender_timestamp", time.time()), "received",
                                  reception=receive_path(p), echo_key=key, sender_key=sender_key)
            scope_updated = False
            if key:
                for packet_key, scope in self.recent_scopes:
                    if packet_key == key:
                        scope_updated = self.store.record_received_scope(key, scope) or scope_updated
            if kind in ("dm", "room") and not any(k.startswith(target) for k in self.contacts):
                self.contacts[target] = {"public_key": target, "adv_name": target, "type": 3 if kind == "room" else 1, "unknown": True}
                self.store.set_meta("contacts", self.contacts)
                self.changed()
            if mid or scope_updated:
                self.emit("message", {"kind": kind, "target": target})
            self.log(name, p)
        elif name in ("LOGIN_SUCCESS", "LOGIN_FAILED"):
            prefix = p.get("pubkey_prefix") or public(event.attributes).get("pubkey_prefix")
            repeater_matches = [key for key, session in self.repeaters.items()
                                if prefix and key.startswith(prefix) and session["status"] == "logging_in"]
            if len(repeater_matches) == 1:
                key = repeater_matches[0]
                self.repeaters[key]["status"] = "logged_in" if name == "LOGIN_SUCCESS" else "failed"
                task = self.repeater_login_tasks.pop(key, None)
                if task:
                    task.cancel()
                self.changed()
            matches = [key for key, room in self.rooms.items()
                       if prefix and key.startswith(prefix) and room["status"] == "logging_in"]
            if len(matches) == 1:
                room = self.rooms[matches[0]]
                if name == "LOGIN_SUCCESS":
                    acl = p.get("acl_permissions")
                    can_post = (acl & 3) >= 2 if isinstance(acl, int) else p.get("permissions") in (0, 1)
                    room.update(status="logged_in", can_post=can_post, error=None)
                else:
                    room.update(status="failed", can_post=False, error="Anmeldung vom Roomserver abgelehnt.")
                if self.room_login_task:
                    self.room_login_task.cancel()
                    self.room_login_task = None
                self.changed()
            self.log(name, p)
        elif name == "ACK":
            code = p.get("code") or public(event.attributes).get("code")
            if code:
                self.acks.append(code)
                self.store.acknowledge(code)
                for mid, wakeup in self.dm_wakeups.items():
                    if self.store.delivered(mid):
                        wakeup.set()
                self.emit("message", {"ack": code})
            self.log(name, p)
        elif name == "DISCONNECTED":
            self.ready = False
            self.invalidate_rooms()
            for task in self.pending_dms.values():
                task.cancel()
            self.status = "reconnecting" if self.desired else "offline"
            self.changed()
        elif name in ("RX_LOG_DATA", "RAW_DATA", "ADVERTISEMENT", "NEW_CONTACT", "PATH_UPDATE", "TRACE_DATA", "DISCOVER_RESPONSE"):
            if name == "RX_LOG_DATA" and (echo := repeater_echo(p)):
                self.recent_echoes.append(echo)
                if self.store.record_repeater(*echo):
                    self.emit("message", {"repeater_echo": True})
            self.log(name, p)

    def remember_discovery(self, name, payload):
        key = payload.get("public_key") or payload.get("pubkey") or payload.get("adv_key")
        if not isinstance(key, str) or len(key) not in (16, 64) or any(c not in "0123456789abcdefABCDEF" for c in key):
            return
        key = key.lower()
        now = time.time()
        entry = self.discovered.setdefault(key, {"public_key": key, "first_seen": now, "sources": []})
        source = "DISCOVER" if name == "DISCOVER_RESPONSE" else "ADVERT"
        if source not in entry["sources"]:
            entry["sources"].append(source)
        entry["last_seen"] = now
        entry.setdefault("observations", {})[name] = {"time": now, "payload": payload}
        contact = {**self.contacts.get(key, {}), **(entry.get("contact") or {})}
        if name == "NEW_CONTACT":
            contact.update(payload)
        else:
            for field in ("adv_name", "adv_lat", "adv_lon"):
                if payload.get(field) is not None:
                    contact[field] = payload[field]
            if isinstance(payload.get("adv_timestamp"), int):
                contact["last_advert"] = payload["adv_timestamp"]
            kind = payload.get("node_type", payload.get("adv_type"))
            if kind in (1, 2, 3, 4):
                contact["type"] = kind
        contact["public_key"] = key
        entry["contact"] = contact
        self.store.set_meta("discovered", self.discovered)
        self.changed()

    async def discover(self):
        async with self.lock:
            self.require_ready()
            await self.command("send_node_discover_req", 0x1E, False)
            self.log("DISCOVER_SENT", {})
            return self.snapshot()

    async def save_discovered(self, key):
        async with self.lock:
            self.require_ready()
            entry = self.discovered.get(key)
            if not entry:
                raise HTTPException(404, "Gerät nicht in der Fundliste.")
            # Read first: never overwrite a contact's flags or route with discovery data.
            result = await self.command("get_contacts")
            self.contacts = public(result.payload)
            self.store.set_meta("contacts", self.contacts)
            if key not in self.contacts:
                contact = dict(entry.get("contact") or {})
                if len(key) != 64 or contact.get("type") not in (1, 2, 3, 4):
                    raise HTTPException(409, "Vollständiger Geräteschlüssel und Gerätetyp fehlen. Erneut DISCOVER senden oder ein ADVERT abwarten.")
                defaults = {"flags": 0, "out_path": "", "out_path_len": -1, "out_path_hash_mode": 0,
                            "last_advert": 0, "adv_lat": 0, "adv_lon": 0, "adv_name": key[:12]}
                contact = {**defaults, **contact, "public_key": key}
                contact["adv_name"] = contact["adv_name"].encode("utf-8")[:31].decode("utf-8", "ignore")
                await self.command("add_contact", contact)
                result = await self.command("get_contacts")
                self.contacts = public(result.payload)
                self.store.set_meta("contacts", self.contacts)
                if key not in self.contacts:
                    self.changed()
                    raise HTTPException(502, "Kontaktübernahme vom Companion nicht bestätigt. Bitte aktualisieren.")
            self.changed()
            return self.snapshot()

    async def command(self, method, *args):
        result = await asyncio.wait_for(getattr(self.radio.commands, method)(*args), 10)
        if result is None or result.type == EventType.ERROR:
            raise RuntimeError(f"{method}: {public(result.payload) if result else 'Keine Antwort vom Companion'}")
        return result

    async def read_contacts(self):
        result = await self.command("get_contacts")
        self.contacts = public(result.payload)
        self.store.set_meta("contacts", self.contacts)
        self.changed()

    async def change_contact(self, setting, remove=False):
        key = setting.target.lower()
        if not remove:
            name = setting.name.strip()
            if not name or len(name.encode("utf-8")) > 31 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise HTTPException(422, "Der Name muss 1–31 UTF-8-Bytes lang sein und darf keine Steuerzeichen enthalten.")
        async with self.lock:
            self.require_ready()
            await self.read_contacts()
            if remove:
                # Keep retries and room login callbacks from using a deleted contact.
                pending = self.store.db.execute(
                    "SELECT 1 FROM messages WHERE target=? AND direction='out' AND status='sending' LIMIT 1",
                    (key[:12],)).fetchone()
                if pending or self.rooms.get(key, {}).get("status") == "logging_in":
                    raise HTTPException(409, "Bitte den laufenden Versand oder die Roomserver-Anmeldung abwarten.")
                if key in self.contacts:
                    await self.command("remove_contact", key)
                await self.read_contacts()
                if key in self.contacts:
                    raise HTTPException(502, "Löschen vom Companion nicht bestätigt. Bitte aktualisieren.")
                self.rooms.pop(key, None)
            else:
                if key in self.contacts:
                    raise HTTPException(409, "Dieser Geräteschlüssel ist bereits im Kontaktbuch gespeichert.")
                contact = {"public_key": key, "adv_name": name, "type": setting.type,
                           "flags": 0, "out_path": "", "out_path_len": -1, "out_path_hash_mode": 0,
                           "last_advert": 0, "adv_lat": 0, "adv_lon": 0}
                await self.command("add_contact", contact)
                await self.read_contacts()
                saved = self.contacts.get(key, {})
                if saved.get("adv_name") != name or saved.get("type") != setting.type:
                    raise HTTPException(502, "Kontakt vom Companion nicht wie angefordert bestätigt. Bitte aktualisieren.")
            self.changed()
            return self.snapshot()

    async def refresh_locked(self):
        await self.read_device()
        await self.read_self_info()
        result = await self.command("get_contacts")
        self.contacts = public(result.payload)
        self.store.set_meta("contacts", self.contacts)
        await self.read_channels()
        try:
            await self.read_default_scope()
        except (RuntimeError, TimeoutError):
            self.default_scope, self.scope_supported = None, False
        self.changed()

    async def read_channels(self):
        channels = []
        slots = {}
        for index in range(min(int(self.device.get("max_channels", 8)), 256)):
            result = await self.command("get_channel", index)
            p = result.payload
            slots[index] = p
            if p.get("channel_name") or any(p.get("channel_secret", b"")):
                channels.append({"index": index, "name": p.get("channel_name") or ("Public" if index == 0 else f"Channel {index}")})
        current_names = {c["index"]: c["name"] for c in channels}
        for previous in self.channels:
            if current_names.get(previous["index"]) != previous["name"]:
                self.store.archive_channel(previous["index"])
                self.channel_scopes.pop(str(previous["index"]), None)
        self.store.set_meta("channel_scopes", self.channel_scopes)
        self.channels = channels
        self.channel_hashes = {str(i): hashlib.sha256(p["channel_secret"]).hexdigest()[:2]
                               for i, p in slots.items() if isinstance(p.get("channel_secret"), bytes)}
        self.channel_names = {str(i): p.get("channel_name", "") for i, p in slots.items()}
        self.store.set_meta("channels", channels)
        self.changed()
        return slots

    async def change_channel(self, setting, remove=False):
        name = normalize_channel(setting.name)
        async with self.lock:
            self.require_ready()
            slots = await self.read_channels()
            if remove:
                index = setting.index
                if index not in slots or slots[index].get("channel_name") != name:
                    raise HTTPException(409, "Channel wurde inzwischen geändert. Bitte aktualisieren.")
                expected_name, expected_secret = "", bytes(16)
            else:
                if any(p.get("channel_name") == name for p in slots.values()):
                    raise HTTPException(409, "Dieser Channel ist bereits im Companion gespeichert.")
                index = next((i for i, p in slots.items() if not p.get("channel_name") and p.get("channel_secret") == bytes(16)), None)
                if index is None:
                    raise HTTPException(409, "Im Companion ist kein freier Channel-Platz vorhanden.")
                expected_name = name
                expected_secret = hashlib.sha256(name.encode("utf-8")).digest()[:16]
            # Empty the device inbox before reusing slots, so buffered messages
            # cannot be assigned to a subsequently created channel.
            for _ in range(1000):
                if (await self.command("get_msg")).type == EventType.NO_MORE_MSGS:
                    break
            else:
                raise HTTPException(409, "Nachrichtenpuffer noch nicht leer. Bitte erneut versuchen.")
            try:
                await self.command("set_channel", index, expected_name, expected_secret)
                result = await self.command("get_channel", index)
                if result.payload.get("channel_name") != expected_name or result.payload.get("channel_secret") != expected_secret:
                    raise RuntimeError("Der Companion hat die Channel-Änderung nicht bestätigt.")
            except (RuntimeError, TimeoutError, ConnectionError):
                # A timed-out write may have reached the radio. Block sends
                # until the authoritative configuration is read again.
                self.ready, self.status = False, "reconnecting"
                self.changed()
                raise
            self.store.archive_channel(index)
            self.channel_hashes[str(index)] = hashlib.sha256(expected_secret).hexdigest()[:2]
            self.channel_names[str(index)] = expected_name
            self.channel_scopes.pop(str(index), None)
            self.store.set_meta("channel_scopes", self.channel_scopes)
            self.channels = [c for c in self.channels if c["index"] != index]
            if not remove:
                self.channels.append({"index": index, "name": name})
                self.channels.sort(key=lambda c: c["index"])
            self.store.set_meta("channels", self.channels)
            self.log("CHANNEL_REMOVED" if remove else "CHANNEL_ADDED", {"index": index, "name": name})
            self.changed()
            return self.snapshot()

    async def read_device(self):
        result = await self.command("send_device_query")
        self.device = public(result.payload)

    async def read_self_info(self):
        result = await self.command("send_appstart")
        self.info = public(result.payload)

    async def save_multi_acks(self, setting):
        async with self.lock:
            self.require_ready()
            await self.read_device()
            await self.read_self_info()
            limits = {"multi_acks": 1, "manual_add_contacts": 1, "adv_loc_policy": 2,
                      "telemetry_mode_base": 3, "telemetry_mode_loc": 3, "telemetry_mode_env": 3}
            if int(self.device.get("fw ver", 0)) < 7 or any(
                (type(self.info.get(key)) not in (int, bool) if key == "manual_add_contacts"
                 else type(self.info.get(key)) is not int) or not 0 <= self.info[key] <= maximum
                for key, maximum in limits.items()
            ):
                raise HTTPException(409, "ACK-Einstellung oder Companion-Parameter nicht verfügbar.")
            # SET_OTHER_PARAMS writes several settings at once. Preserve the
            # values freshly read from the companion, including telemetry.
            updated = {**self.info, "multi_acks": int(setting.enabled)}
            try:
                await self.command("set_other_params_from_infos", updated)
                await self.read_self_info()
                if self.info.get("multi_acks") != int(setting.enabled):
                    raise RuntimeError("Der Companion hat die ACK-Einstellung nicht bestätigt.")
            except (RuntimeError, TimeoutError, ConnectionError):
                self.info.pop("multi_acks", None)
                self.ready, self.status = False, "reconnecting"
                self.changed()
                raise
            self.log("MULTI_ACKS_UPDATED", {"enabled": setting.enabled})
            self.changed()
            return self.snapshot()

    async def save_path_hash(self, setting):
        async with self.lock:
            self.require_ready()
            await self.read_device()
            mode = self.device.get("path_hash_mode")
            if type(mode) is not int or mode not in (0, 1, 2):
                raise HTTPException(409, "Diese Firmware unterstützt keine einstellbare Pfad-Hash-Länge.")
            try:
                await self.command("set_path_hash_mode", setting.bytes - 1)
                await self.read_device()
                if self.device.get("path_hash_mode") != setting.bytes - 1:
                    raise RuntimeError("Der Companion hat eine andere Pfad-Hash-Länge zurückgemeldet.")
            except (RuntimeError, TimeoutError, ConnectionError):
                self.device.pop("path_hash_mode", None)
                self.ready, self.status = False, "reconnecting"
                self.changed()
                raise
            self.log("PATH_HASH_UPDATED", {"bytes": setting.bytes})
            self.changed()
            return self.snapshot()

    async def read_default_scope(self):
        result = await self.command("get_default_flood_scope")
        self.default_scope = result.payload.get("scope_name", "")
        self.scope_supported = True

    async def save_scope(self, setting):
        scope = normalize_scope(setting.scope)
        async with self.lock:
            self.require_ready()
            if setting.channel is not None:
                if setting.channel not in {str(c["index"]) for c in self.channels}:
                    raise HTTPException(404, "Channel nicht gefunden.")
                if int(self.device.get("fw ver", 0)) < 8:
                    raise HTTPException(409, "Diese Firmware unterstützt keine Channel-Scopes.")
                if scope == "*" and int(self.device.get("fw ver", 0)) < 12:
                    raise HTTPException(409, "Diese Firmware unterstützt kein erzwungenes Senden ohne Scope.")
                self.channel_scopes[setting.channel] = scope
                self.store.set_meta("channel_scopes", self.channel_scopes)
            else:
                if not self.scope_supported:
                    raise HTTPException(409, "Standard-Scope konnte von dieser Firmware nicht gelesen werden.")
                # meshcore 2.3.14 pads by character count and cannot clear the default
                # correctly. Encode the documented 31-byte name field ourselves.
                name = "" if scope == "*" else scope
                encoded = name.encode("utf-8")
                frame = bytes([CommandType.SET_DEFAULT_FLOOD_SCOPE.value])
                if encoded:
                    frame += encoded.ljust(31, b"\0") + hashlib.sha256(encoded).digest()[:16]
                await self.command("send", frame, [EventType.OK, EventType.ERROR])
                try:
                    await self.read_default_scope()
                except (RuntimeError, TimeoutError):
                    self.default_scope, self.scope_supported = None, False
                    self.changed()
                    raise
                if self.default_scope != name:
                    self.changed()
                    raise RuntimeError("Der Companion hat einen anderen Standard-Scope zurückgemeldet.")
            self.log("SCOPE_UPDATED", {"channel": setting.channel, "scope": scope})
            self.changed()
            return self.snapshot()

    async def run(self):
        while True:
            if not self.desired:
                await asyncio.sleep(.3)
                continue
            try:
                self.status, self.error = "connecting", None
                self.changed()
                self.radio = MeshCore(TCPConnection(self.host, self.port), default_timeout=5)
                self.radio.set_decrypt_channel_logs(True)
                for kind in EventType:
                    self.radio.subscribe(kind, self.on_event)
                async with self.lock:
                    result = await asyncio.wait_for(self.radio.connect(), 12)
                    if result is None or result.type == EventType.ERROR:
                        raise ConnectionError("Companion antwortet nicht auf den MeshCore-Handshake.")
                    await self.command("send_device_query")
                    if int(self.device.get("fw ver", 0)) >= 8:
                        await self.command("set_flood_scope", "")
                    await self.refresh_locked()
                    self.ready, self.status = True, "connected"
                    self.log("CONNECTED", {"address": f"{self.host}:{self.port}"})
                    self.changed()
                next_stats = 0
                while self.desired and self.ready and self.radio.is_connected:
                    async with self.lock:
                        # Polling also drains messages queued before the TCP connection.
                        for _ in range(100):
                            result = await self.command("get_msg")
                            if result.type == EventType.NO_MORE_MSGS:
                                break
                        if time.monotonic() >= next_stats:
                            for method in ("get_stats_core", "get_stats_radio", "get_stats_packets"):
                                try:
                                    await self.command(method)
                                except (RuntimeError, TimeoutError):
                                    pass  # Older companion firmware may not expose statistics.
                            next_stats = time.monotonic() + 15
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc) or type(exc).__name__
                self.log("CONNECTION_ERROR", {"message": self.error})
            finally:
                self.ready = False
                self.invalidate_rooms()
                await self.stop_dms()
                if self.radio:
                    with suppress(Exception):
                        await asyncio.wait_for(self.radio.disconnect(), 5)
                self.radio = None
                self.status = "reconnecting" if self.desired else "offline"
                self.changed()
            await asyncio.sleep(5 if self.desired else .1)

    def require_ready(self):
        if not self.ready or not self.radio or not self.radio.is_connected:
            raise HTTPException(409, "Der Companion ist nicht verbunden.")

    def require_repeater(self, target):
        contact = self.contacts.get(target)
        if not contact or contact.get("type") != 2 or contact.get("unknown"):
            raise HTTPException(404, "Kein gespeicherter Repeater-Kontakt.")
        return contact

    async def repeater_login_expiry(self, target, timeout):
        try:
            await asyncio.sleep(timeout)
            self.repeaters[target]["status"] = "timeout"
            self.repeater_login_tasks.pop(target, None)
            self.changed()
        except asyncio.CancelledError:
            pass

    async def repeater_action(self, target, action, value=""):
        if action == "login" and ("\x00" in value or len(value.encode("utf-8")) > 15):
            raise HTTPException(422, "Passwort: höchstens 15 UTF-8-Bytes, keine Nullzeichen.")
        if action == "command" and (not value.strip() or len(value.encode("utf-8")) > 160
                                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise HTTPException(422, "Kommando: 1–160 UTF-8-Bytes, keine Zeilenumbrüche oder Steuerzeichen.")
        async with self.lock:
            self.require_ready()
            contact = self.require_repeater(target)
            session = self.repeaters.setdefault(target, {"status": "disconnected", "replies": []})
            if session["status"] == "logging_in":
                raise HTTPException(409, "Bitte die laufende Anmeldung abwarten.")
            try:
                if action == "login":
                    session["status"] = "logging_in"
                    self.changed()
                    result = await self.command("send_login", target, value)
                    if session["status"] == "logging_in":
                        timeout = max(10.0, result.payload.get("suggested_timeout", 10000) / 1000 * 1.2)
                        self.repeater_login_tasks[target] = asyncio.create_task(self.repeater_login_expiry(target, timeout))
                elif action == "logout":
                    await self.command("send_logout", target)
                    session.update(status="disconnected", replies=[])
                else:
                    if session["status"] != "logged_in":
                        raise HTTPException(409, "Zuerst am Repeater anmelden.")
                    await self.command("send_cmd", {**contact, "public_key": target}, value)
            except (RuntimeError, TimeoutError, ConnectionError):
                if action == "login":
                    session["status"] = "failed"
                self.changed()
                raise HTTPException(502, "Repeater-Anfrage vom Companion nicht bestätigt.") from None
            self.changed()
            return self.snapshot()

    def require_room(self, target):
        contact = self.contacts.get(target)
        if not contact or contact.get("type") != 3 or contact.get("unknown"):
            raise HTTPException(404, "Kein gespeicherter Roomserver-Kontakt.")
        return contact

    def invalidate_rooms(self):
        for task in self.repeater_login_tasks.values():
            task.cancel()
        self.repeater_login_tasks.clear()
        for session in self.repeaters.values():
            session["status"] = "disconnected"
        if self.room_login_task:
            self.room_login_task.cancel()
            self.room_login_task = None
        for room in self.rooms.values():
            room.update(status="disconnected", can_post=False, error=None)

    async def room_login_expiry(self, target, timeout):
        try:
            await asyncio.sleep(timeout)
            room = self.rooms[target]
            if room["status"] == "logging_in":
                room.update(status="failed", can_post=False,
                            error="Keine Anmeldebestätigung. Passwort und Erreichbarkeit prüfen.")
                self.changed()
        except asyncio.CancelledError:
            pass

    async def login_room(self, target, password):
        if "\x00" in password or len(password.encode("utf-8")) > 15:
            raise HTTPException(422, "Das Room-Passwort darf höchstens 15 UTF-8-Bytes und keine Nullzeichen enthalten.")
        async with self.lock:
            self.require_ready()
            self.require_room(target)
            if any(room["status"] == "logging_in" for room in self.rooms.values()):
                raise HTTPException(409, "Eine Roomserver-Anmeldung läuft bereits.")
            if target in self.room_sends:
                raise HTTPException(409, "Bitte den laufenden Room-Beitrag abwarten.")
            self.rooms[target] = {"status": "logging_in", "can_post": False, "error": None}
            self.changed()
            try:
                result = await self.command("send_login", target, password)
            except (RuntimeError, TimeoutError, ConnectionError):
                # Never return command details that could contain credentials.
                self.rooms[target].update(status="failed", error="Anmeldung konnte nicht gesendet werden.")
                self.changed()
                raise HTTPException(502, "Anmeldung konnte nicht gesendet werden.") from None
            if self.rooms[target]["status"] == "logging_in":
                timeout = max(10.0, result.payload.get("suggested_timeout", 10000) / 1000 * 1.2)
                self.room_login_task = asyncio.create_task(self.room_login_expiry(target, timeout))
            return self.snapshot()

    async def logout_room(self, target):
        async with self.lock:
            self.require_ready()
            self.require_room(target)
            await self.command("send_logout", target)
            if self.rooms.get(target, {}).get("status") == "logging_in" and self.room_login_task:
                self.room_login_task.cancel()
                self.room_login_task = None
            mid = self.room_sends.get(target)
            if mid in self.pending_dms:
                self.pending_dms[mid].cancel()
            self.rooms[target] = {"status": "disconnected", "can_post": False, "error": None}
            self.changed()
            return self.snapshot()

    async def stop_dms(self):
        tasks = list(self.pending_dms.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def dm_done(self, mid):
        # Also covers cancellation before the coroutine's first execution.
        if mid in self.pending_dms:
            self.store.finish_send(mid, "interrupted")
            self.pending_dms.pop(mid, None)
            self.dm_wakeups.pop(mid, None)
            self.emit("message", {"id": mid})
        for target, pending in list(self.room_sends.items()):
            if pending == mid:
                del self.room_sends[target]
                self.changed()

    def dm_attempt(self, mid, result, route, attempt):
        payload = public(result.payload)
        code = payload.get("expected_ack")
        self.store.record_attempt(mid, code, route, attempt)
        if code and code in self.acks:
            self.store.acknowledge(code)
        self.emit("message", {"id": mid})

    async def wait_dm_ack(self, mid, result):
        # The companion estimates airtime and route latency in milliseconds.
        timeout = max(5.0, result.payload.get("suggested_timeout", 10000) / 1000 * 1.2)
        if not self.store.delivered(mid):
            with suppress(TimeoutError):
                await asyncio.wait_for(self.dm_wakeups[mid].wait(), timeout)

    async def retry_dm(self, mid, target, text, timestamp, result, radio):
        route = "flood" if result.payload.get("type") == 1 else "direct"
        direct_count, flood_count = (0, 1) if route == "flood" else (1, 0)
        attempt = 0
        try:
            while True:
                await self.wait_dm_ack(mid, result)
                if self.store.delivered(mid):
                    return
                if flood_count >= 3:
                    self.store.finish_send(mid, "unconfirmed")
                    return
                async with self.lock:
                    if self.store.delivered(mid):
                        return
                    self.require_ready()
                    if self.radio is not radio:
                        raise ConnectionError("Companion-Verbindung gewechselt")
                    contact = self.contacts[target]
                    if contact.get("type") == 3 and not self.rooms.get(target, {}).get("can_post"):
                        raise RuntimeError("Roomserver-Anmeldung nicht mehr aktiv")
                    if direct_count >= 3 or flood_count:
                        # Reset on each flood attempt: a PATH_UPDATE may have
                        # supplied a new direct route while waiting for an ACK.
                        await self.command("reset_path", target)
                        contact.update(out_path="", out_path_len=-1)
                        self.store.set_meta("contacts", self.contacts)
                        self.changed()
                    if self.store.delivered(mid):
                        return
                    attempt += 1
                    result = await self.command("send_msg", contact, text, timestamp, attempt)
                    route = "flood" if result.payload.get("type") == 1 else "direct"
                    if route == "flood":
                        flood_count += 1
                    else:
                        direct_count += 1
                    self.dm_attempt(mid, result, route, flood_count if route == "flood" else direct_count)
                    self.log("MESSAGE_RETRY", {"id": mid, "route": route, "attempt": attempt + 1})
                    # A device that ignores reset_path must not cause an endless loop.
                    if direct_count > 3:
                        raise RuntimeError("Companion hat den Wechsel zu Flood nicht bestätigt")
        except asyncio.CancelledError:
            self.store.finish_send(mid, "interrupted")
            raise
        except (RuntimeError, TimeoutError, ConnectionError, HTTPException, KeyError) as exc:
            self.store.finish_send(mid, "interrupted")
            self.log("MESSAGE_RETRY_ERROR", {"id": mid, "message": str(exc) or type(exc).__name__})
        finally:
            self.emit("message", {"id": mid})
            self.pending_dms.pop(mid, None)
            self.dm_wakeups.pop(mid, None)

    async def send(self, kind, target, text):
        text = text.strip()
        if not text or "\x00" in text:
            raise HTTPException(422, "Bitte eine Nachricht ohne Nullzeichen eingeben.")
        # Conservative limit with room for the channel sender name and framing.
        if len(text.encode("utf-8")) > 160:
            raise HTTPException(422, "Die Nachricht darf höchstens 160 UTF-8-Bytes enthalten.")
        async with self.lock:
            self.require_ready()
            if kind == "channel" and self.info.get("name"):
                limit = max(0, 160 - len(f"{self.info['name']}: ".encode("utf-8")))
                if len(text.encode("utf-8")) > limit:
                    raise HTTPException(422, f"Mit deinem Absendernamen passen höchstens {limit} UTF-8-Bytes in eine Channel-Nachricht.")
            timestamp = int(time.time())
            echo_key = None
            if kind == "channel":
                if target not in {str(c["index"]) for c in self.channels}:
                    raise HTTPException(404, "Channel nicht gefunden.")
                if target in self.channel_hashes and self.info.get("name"):
                    channel_name = self.channel_names.get(target, next(c["name"] for c in self.channels if str(c["index"]) == target))
                    echo_key = channel_echo_key(channel_name, self.channel_hashes[target], timestamp,
                                                f"{self.info['name']}: {text}")
                scope = self.channel_scopes.get(target, "")
                supports_scope = int(self.device.get("fw ver", 0)) >= 8
                if scope and not supports_scope:
                    raise HTTPException(409, "Die Firmware unterstützt den gespeicherten Scope nicht.")
                if scope == "*" and int(self.device.get("fw ver", 0)) < 12:
                    raise HTTPException(409, "Die Firmware unterstützt kein Senden ohne Scope.")
                try:
                    if supports_scope:
                        await self.command("set_flood_scope", scope)
                    result = await self.command("send_chan_msg", int(target), text, timestamp)
                finally:
                    if supports_scope:
                        try:
                            await self.command("set_flood_scope", "")
                        except (RuntimeError, TimeoutError, ConnectionError):
                            # Block subsequent sends until a fresh session is ready.
                            self.ready = False
                            self.status = "reconnecting"
                            self.log("SCOPE_RESET_ERROR", {"message": "Scope-Rücksetzung unbestätigt; Neuverbindung erforderlich."})
                            self.changed()
            else:
                dm_target = target
                contact = self.contacts.get(target)
                if kind == "room":
                    contact = self.require_room(target)
                    if not self.rooms.get(target, {}).get("can_post"):
                        raise HTTPException(409, "Zuerst mit Schreibberechtigung am Roomserver anmelden.")
                    if target in self.room_sends:
                        raise HTTPException(409, "Bitte die Bestätigung des laufenden Room-Beitrags abwarten.")
                    if len(text.encode("utf-8")) > 150:
                        raise HTTPException(422, "Ein Room-Beitrag darf höchstens 150 UTF-8-Bytes enthalten.")
                    # Rooms reject older timestamps and consider equal ones a retry.
                    clock = await self.command("get_time")
                    timestamp = max(int(clock.payload["time"]) + 1, self.room_timestamps.get(target, 0) + 1)
                    self.room_timestamps[target] = timestamp
                elif not contact or contact.get("unknown") or contact.get("type") != 1:
                    raise HTTPException(404, "Kein gespeicherter Chat-Kontakt für diese Direktnachricht.")
                result = await self.command("send_msg", contact, text, timestamp)
                target = target[:12]
            ack = public(result.payload).get("expected_ack") if kind in ("dm", "room") else None
            status = "delivered" if ack and ack in self.acks else "sent"
            mid = self.store.save(kind, target, "out", text, timestamp, status, ack, echo_key=echo_key)
            if kind in ("dm", "room"):
                route = "flood" if result.payload.get("type") == 1 else "direct"
                self.dm_attempt(mid, result, route, 1)
                status = "delivered" if self.store.delivered(mid) else "sending"
                if status != "delivered":
                    if kind == "room":
                        self.room_sends[dm_target] = mid
                    self.dm_wakeups[mid] = asyncio.Event()
                    self.pending_dms[mid] = asyncio.create_task(
                        self.retry_dm(mid, dm_target, text, timestamp, result, self.radio))
                    self.pending_dms[mid].add_done_callback(lambda task, mid=mid: self.dm_done(mid))
                    if kind == "room":
                        self.changed()
            if echo_key:
                for key, hop in self.recent_echoes:
                    if key == echo_key:
                        self.store.record_repeater(key, hop)
            self.emit("message", {"kind": kind, "target": target})
            self.log("MESSAGE_SENT", {"kind": kind, "target": target, "text": text})
            return {"id": mid, "status": status}


class MessageInput(BaseModel):
    kind: Literal["channel", "dm", "room"]
    target: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=160)


class RoomTarget(BaseModel):
    target: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class ContactInput(RoomTarget):
    name: str = Field(min_length=1, max_length=100)
    type: int = Field(strict=True, ge=1, le=4)


class RepeaterAction(RoomTarget):
    action: Literal["login", "logout", "command"]
    value: SecretStr = SecretStr("")


class RoomLogin(RoomTarget):
    password: SecretStr = SecretStr("")


def normalize_scope(value):
    value = value.strip()
    if value in ("", "*"):
        return value
    if not value.startswith("#"):
        value = "#" + value
    if len(value) < 2 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise HTTPException(422, "Scope-Namen dürfen keine Leer- oder Steuerzeichen enthalten.")
    if len(value.encode("utf-8")) > 30:
        raise HTTPException(422, "Der Scope darf einschließlich # höchstens 30 UTF-8-Bytes lang sein.")
    return value


class ScopeInput(BaseModel):
    channel: str | None = Field(default=None, max_length=3)
    scope: str = Field(max_length=100)


class PathHashInput(BaseModel):
    bytes: int = Field(strict=True, ge=1, le=3)


class MultiAcksInput(BaseModel):
    enabled: bool = Field(strict=True)


def normalize_channel(value):
    value = value.strip()
    if not value.startswith("#"):
        value = "#" + value
    if len(value) < 2 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "#" for c in value[1:]):
        raise HTTPException(422, "Bitte einen Hashtag-Namen ohne Leerzeichen oder weitere # eingeben.")
    if len(value.encode("utf-8")) > 31:
        raise HTTPException(422, "Der Channel-Name darf mit # höchstens 31 UTF-8-Bytes enthalten.")
    return value


class ChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ChannelRemoveInput(ChannelInput):
    index: int = Field(ge=0, le=255)


def create_app(db_path=None, autoconnect=None):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(db_path or os.getenv("MESHCORE_DB", "data/messages.sqlite3"))
        bridge = Bridge(store, os.getenv("MESHCORE_HOST", "192.168.88.14"), int(os.getenv("MESHCORE_PORT", "5000")))
        app.state.bridge = bridge
        bridge.desired = autoconnect if autoconnect is not None else os.getenv("MESHCORE_AUTOCONNECT", "1") == "1"
        bridge.task = asyncio.create_task(bridge.run())
        yield
        bridge.desired = False
        bridge.task.cancel()
        with suppress(asyncio.CancelledError):
            await bridge.task
        await bridge.stop_dms()
        store.db.close()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def protect_local_api(request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith("/api/"):
            # A custom header requires a CORS preflight from foreign websites.
            if request.headers.get("x-meshcore-client") != "web":
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Client-Header fehlt."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    @app.get("/api/state")
    async def state(request: Request):
        return request.app.state.bridge.snapshot()

    @app.post("/api/connect")
    async def connect(request: Request):
        bridge = request.app.state.bridge
        bridge.desired = True
        return {"status": "connecting"}

    @app.post("/api/refresh")
    async def refresh(request: Request):
        bridge = request.app.state.bridge
        async with bridge.lock:
            bridge.require_ready()
            try:
                await bridge.refresh_locked()
            except (RuntimeError, TimeoutError) as exc:
                raise HTTPException(502, str(exc) or "Companion-Timeout") from exc
        return bridge.snapshot()

    @app.post("/api/discover")
    async def discover(request: Request):
        try:
            return await request.app.state.bridge.discover()
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise HTTPException(502, f"DISCOVER nicht bestätigt: {str(exc) or 'Timeout'}") from exc

    @app.post("/api/discovered/save")
    async def save_discovered(request: Request, setting: RoomTarget):
        try:
            return await request.app.state.bridge.save_discovered(setting.target.lower())
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise HTTPException(502, "Kontaktübernahme unbestätigt. Bitte aktualisieren.") from exc

    async def contact_change(request, setting, remove=False):
        try:
            return await request.app.state.bridge.change_contact(setting, remove)
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise HTTPException(502, "Kontaktänderung unbestätigt. Bitte aktualisieren.") from exc

    @app.post("/api/contacts")
    async def add_contact(request: Request, setting: ContactInput):
        return await contact_change(request, setting)

    @app.post("/api/contacts/remove")
    async def remove_contact(request: Request, setting: RoomTarget):
        return await contact_change(request, setting, True)

    @app.get("/api/messages")
    async def messages(request: Request, kind: Literal["channel", "dm", "room"], target: str, before: int | None = None):
        return request.app.state.bridge.store.history(kind, target[:12] if kind in ("dm", "room") else target, before)

    @app.post("/api/repeaters")
    async def repeater_action(request: Request, setting: RepeaterAction):
        return await request.app.state.bridge.repeater_action(
            setting.target.lower(), setting.action, setting.value.get_secret_value())

    @app.post("/api/rooms/login")
    async def room_login(request: Request, setting: RoomLogin):
        return await request.app.state.bridge.login_room(setting.target.lower(), setting.password.get_secret_value())

    @app.post("/api/rooms/logout")
    async def room_logout(request: Request, setting: RoomTarget):
        try:
            return await request.app.state.bridge.logout_room(setting.target.lower())
        except (RuntimeError, TimeoutError, ConnectionError):
            raise HTTPException(502, "Lokale Abmeldung vom Companion nicht bestätigt.") from None

    @app.post("/api/path-hash")
    async def path_hash(request: Request, setting: PathHashInput):
        try:
            return await request.app.state.bridge.save_path_hash(setting)
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise HTTPException(502, f"Pfad-Hash-Länge unbestätigt: {str(exc) or 'Timeout'}. Nach Neuverbindung prüfen.") from exc

    @app.post("/api/multi-acks")
    async def multi_acks(request: Request, setting: MultiAcksInput):
        try:
            return await request.app.state.bridge.save_multi_acks(setting)
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise HTTPException(502, "ACK-Einstellung unbestätigt. Nach Neuverbindung prüfen.") from exc

    @app.post("/api/scopes")
    async def scopes(request: Request, setting: ScopeInput):
        try:
            return await request.app.state.bridge.save_scope(setting)
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise HTTPException(502, f"Scope konnte nicht bestätigt werden: {str(exc) or 'Timeout'}. Bitte aktualisieren.") from exc

    async def channel_change(request, setting, remove):
        try:
            return await request.app.state.bridge.change_channel(setting, remove)
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise HTTPException(502, f"Channel-Änderung unbestätigt: {str(exc) or 'Timeout'}. Nach Neuverbindung prüfen.") from exc

    @app.post("/api/channels")
    async def add_channel(request: Request, setting: ChannelInput):
        return await channel_change(request, setting, False)

    @app.post("/api/channels/remove")
    async def remove_channel(request: Request, setting: ChannelRemoveInput):
        return await channel_change(request, setting, True)

    @app.post("/api/messages")
    async def send(request: Request, message: MessageInput):
        try:
            return await request.app.state.bridge.send(message.kind, message.target, message.text)
        except (RuntimeError, TimeoutError, ConnectionError) as exc:
            raise HTTPException(502, f"Sendestatus unklar: {str(exc) or 'Timeout'}. Vor erneutem Senden prüfen.") from exc

    @app.get("/api/events")
    async def events(request: Request):
        bridge = request.app.state.bridge
        async def stream():
            queue = asyncio.Queue(maxsize=100)
            bridge.listeners.add(queue)
            try:
                yield f"event: state\ndata: {json.dumps(bridge.snapshot())}\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), 15)
                        yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                bridge.listeners.discard(queue)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static", html=True), name="static")
    return app


app = create_app()
