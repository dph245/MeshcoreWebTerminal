"""Local MeshCore TCP bridge, persistent chat history and live event stream."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
import hashlib
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
from pydantic import BaseModel, Field


def public(value):
    """JSON-safe payload without device PINs or channel secrets."""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): public(v) for k, v in value.items()
                if not any(s in str(k).lower() for s in ("secret", "private", "pin"))}
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

    def save(self, kind, target, direction, text, timestamp, status, ack=None, reception=None):
        fingerprint = None
        if direction == "in":
            fingerprint = hashlib.sha256(json.dumps(
                [kind, target, text, timestamp], ensure_ascii=False).encode()).hexdigest()
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO messages(kind,target,direction,text,timestamp,status,ack,fingerprint,reception) VALUES(?,?,?,?,?,?,?,?,?)",
                (kind, target, direction, text, timestamp, status, ack, fingerprint,
                 json.dumps(reception) if reception is not None else None))
        return cursor.lastrowid if cursor.rowcount else None

    def history(self, kind, target, before=None):
        rows = self.db.execute(
            "SELECT * FROM messages WHERE kind=? AND target=? AND id<? ORDER BY id DESC LIMIT 100",
            (kind, target, before or 9223372036854775807)).fetchall()
        result = [dict(row) for row in reversed(rows)]
        for message in result:
            message["reception"] = json.loads(message["reception"]) if message["reception"] else None
        return result

    def acknowledge(self, code):
        with self.db:
            self.db.execute("UPDATE messages SET status='delivered' WHERE ack=? AND direction='out'", (code,))

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
        self.info = {}
        self.device = {}
        self.stats = {}
        self.default_scope = None
        self.scope_supported = False
        self.channel_scopes = store.get_meta("channel_scopes", {})
        self.events = deque(maxlen=300)
        self.acks = deque(maxlen=200)
        self.listeners = set()
        self.task = None

    def snapshot(self):
        return public(dict(status=self.status, error=self.error, host=self.host, port=self.port,
                           channels=self.channels, contacts=self.contacts, info=self.info,
                           device=self.device, stats=self.stats, events=list(self.events),
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
        self.events.append(event)
        self.emit("radio", event)

    async def on_event(self, event):
        p = public(event.payload or {})
        name = event.type.name
        if name == "SELF_INFO":
            self.info = p
        elif name == "DEVICE_INFO":
            self.device = p
        elif name.startswith("STATS_"):
            self.stats[name] = p
            self.changed()
        elif name in ("CONTACT_MSG_RECV", "CHANNEL_MSG_RECV"):
            kind = "channel" if name == "CHANNEL_MSG_RECV" else "dm"
            target = str(p["channel_idx"]) if kind == "channel" else p["pubkey_prefix"]
            # DMs use the protocol's 6-byte prefix, including for unknown senders.
            mid = self.store.save(kind, target, "in", p.get("text", ""),
                                  p.get("sender_timestamp", time.time()), "received",
                                  reception=receive_path(p))
            if kind == "dm" and not any(k.startswith(target) for k in self.contacts):
                self.contacts[target] = {"public_key": target, "adv_name": target, "type": 1, "unknown": True}
                self.store.set_meta("contacts", self.contacts)
                self.changed()
            if mid:
                self.emit("message", {"kind": kind, "target": target})
            self.log(name, p)
        elif name == "ACK":
            code = p.get("code") or public(event.attributes).get("code")
            if code:
                self.acks.append(code)
                self.store.acknowledge(code)
                self.emit("message", {"ack": code})
            self.log(name, p)
        elif name == "DISCONNECTED":
            self.ready = False
            self.status = "reconnecting" if self.desired else "offline"
            self.changed()
        elif name in ("RX_LOG_DATA", "RAW_DATA", "ADVERTISEMENT", "NEW_CONTACT", "PATH_UPDATE", "TRACE_DATA"):
            self.log(name, p)

    async def command(self, method, *args):
        result = await asyncio.wait_for(getattr(self.radio.commands, method)(*args), 10)
        if result is None or result.type == EventType.ERROR:
            raise RuntimeError(f"{method}: {public(result.payload) if result else 'Keine Antwort vom Companion'}")
        return result

    async def refresh_locked(self):
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

    async def send(self, kind, target, text):
        text = text.strip()
        if not text or "\x00" in text:
            raise HTTPException(422, "Bitte eine Nachricht ohne Nullzeichen eingeben.")
        # Conservative limit with room for the channel sender name and framing.
        if len(text.encode("utf-8")) > 160:
            raise HTTPException(422, "Die Nachricht darf höchstens 160 UTF-8-Bytes enthalten.")
        async with self.lock:
            self.require_ready()
            timestamp = int(time.time())
            if kind == "channel":
                if target not in {str(c["index"]) for c in self.channels}:
                    raise HTTPException(404, "Channel nicht gefunden.")
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
                contact = self.contacts.get(target)
                if not contact or contact.get("unknown") or contact.get("type") != 1:
                    raise HTTPException(404, "Kein gespeicherter Chat-Kontakt für diese Direktnachricht.")
                result = await self.command("send_msg", contact, text, timestamp)
                target = target[:12]
            ack = public(result.payload).get("expected_ack") if kind == "dm" else None
            status = "delivered" if ack and ack in self.acks else "sent"
            mid = self.store.save(kind, target, "out", text, timestamp, status, ack)
            self.emit("message", {"kind": kind, "target": target})
            self.log("MESSAGE_SENT", {"kind": kind, "target": target, "text": text})
            return {"id": mid, "status": status}


class MessageInput(BaseModel):
    kind: Literal["channel", "dm"]
    target: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=160)


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

    @app.get("/api/messages")
    async def messages(request: Request, kind: Literal["channel", "dm"], target: str, before: int | None = None):
        return request.app.state.bridge.store.history(kind, target[:12] if kind == "dm" else target, before)

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
