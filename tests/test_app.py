import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from meshcore import EventType
from meshcore.events import Event

from server.app import Bridge, Store, ScopeInput, create_app, public, normalize_scope


KEY = 'abcdef123456' + 'ab' * 26


@pytest.fixture
def bridge():
    store = Store(':memory:')
    bridge = Bridge(store, '192.168.88.14', 5000)
    bridge.ready = True
    bridge.status = 'connected'
    bridge.channels = [{'index': 0, 'name': 'Public'}]
    bridge.contacts = {KEY: {'public_key': KEY, 'adv_name': 'Test', 'type': 1}}
    bridge.radio = SimpleNamespace(is_connected=True, commands=SimpleNamespace(
        get_default_flood_scope=AsyncMock(return_value=Event(EventType.DEFAULT_FLOOD_SCOPE, {})),
        set_flood_scope=AsyncMock(return_value=Event(EventType.OK, {})),
        send=AsyncMock(return_value=Event(EventType.OK, {})),
        send_chan_msg=AsyncMock(return_value=Event(EventType.OK, {})),
        send_msg=AsyncMock(return_value=Event(EventType.MSG_SENT, {'expected_ack': bytes.fromhex('1234abcd')})),
    ))
    yield bridge
    store.db.close()


def test_history_survives_restart_and_deduplicates(tmp_path):
    path = str(tmp_path / 'messages.db')
    store = Store(path)
    for i in range(105):
        store.save('channel', '0', 'in', f'message {i}', i, 'received')
    assert store.save('channel', '0', 'in', 'message 104', 104, 'received') is None
    store.db.close()
    store = Store(path)
    recent = store.history('channel', '0')
    assert len(recent) == 100
    assert recent[-1]['text'] == 'message 104'
    assert len(store.history('channel', '0', recent[0]['id'])) == 5
    assert store.history('dm', '0') == []
    store.db.close()


def test_channel_send_and_dm_ack(bridge):
    async def scenario():
        await bridge.send('channel', '0', ' Moin! ')
        bridge.radio.commands.send_chan_msg.assert_awaited_once()
        assert bridge.store.history('channel', '0')[0]['text'] == 'Moin!'
        await bridge.send('dm', KEY, 'Hallo')
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'sent'
        await bridge.on_event(Event(EventType.ACK, {'code': '1234abcd'}))
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'delivered'
    asyncio.run(scenario())


def test_early_ack(bridge):
    async def scenario():
        await bridge.on_event(Event(EventType.ACK, {'code': '1234abcd'}))
        await bridge.send('dm', KEY, 'Frühes ACK')
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'delivered'
    asyncio.run(scenario())


@pytest.mark.parametrize('text', [' ', '😀' * 41, 'a\x00b'])
def test_invalid_text_never_sent(bridge, text):
    with pytest.raises(HTTPException) as error:
        asyncio.run(bridge.send('channel', '0', text))
    assert error.value.status_code == 422
    bridge.radio.commands.send_chan_msg.assert_not_awaited()


def test_failed_send_is_not_recorded_as_success(bridge):
    bridge.radio.commands.send_chan_msg.return_value = Event(EventType.ERROR, {'code': 2})
    with pytest.raises(RuntimeError):
        asyncio.run(bridge.send('channel', '0', 'Test'))
    assert bridge.store.history('channel', '0') == []


def test_unknown_channel_and_offline_rejected(bridge):
    with pytest.raises(HTTPException) as error:
        asyncio.run(bridge.send('channel', '7', 'Test'))
    assert error.value.status_code == 404
    bridge.ready = False
    with pytest.raises(HTTPException) as error:
        asyncio.run(bridge.send('channel', '0', 'Test'))
    assert error.value.status_code == 409


def test_incoming_messages_route_by_channel_or_prefix(bridge):
    async def scenario():
        message = Event(EventType.CONTACT_MSG_RECV, {'pubkey_prefix': KEY[:12], 'text': 'Hallo', 'sender_timestamp': 123})
        await bridge.on_event(message)
        await bridge.on_event(message)
        await bridge.on_event(Event(EventType.CHANNEL_MSG_RECV, {'channel_idx': 0, 'text': 'Test: Moin', 'sender_timestamp': 124}))
        assert len(bridge.store.history('dm', KEY[:12])) == 1
        assert bridge.store.history('channel', '0')[0]['text'] == 'Test: Moin'
    asyncio.run(scenario())


def test_secret_redaction():
    assert public({'channel_secret': b'123', 'ble_pin': 1234, 'nested': {'private_key': b'123'}, 'code': b'\xff'}) == {'nested': {}, 'code': 'ff'}


def test_unnamed_public_channel_loaded_without_exposing_key(bridge):
    bridge.device = {'max_channels': 2}
    bridge.radio.commands.get_contacts = AsyncMock(return_value=Event(EventType.CONTACTS, bridge.contacts))
    bridge.radio.commands.get_channel = AsyncMock(side_effect=[
        Event(EventType.CHANNEL_INFO, {'channel_idx': 0, 'channel_name': '', 'channel_secret': b'\x01' * 16}),
        Event(EventType.CHANNEL_INFO, {'channel_idx': 1, 'channel_name': '', 'channel_secret': b'\x00' * 16}),
    ])
    asyncio.run(bridge.refresh_locked())
    assert bridge.channels == [{'index': 0, 'name': 'Public'}]
    assert bridge.store.get_meta('channels', []) == bridge.channels


def test_api_offline_and_write_protection(tmp_path):
    with TestClient(create_app(str(tmp_path / 'api.db'), autoconnect=False)) as client:
        state = client.get('/api/state').json()
        assert state['host'] == '192.168.88.14'
        assert state['status'] == 'offline'
        assert client.get('/').status_code == 200
        assert client.get('/app.js').status_code == 200
        assert client.post('/api/messages', json={'kind': 'channel', 'target': '0', 'text': 'Moin'}).status_code == 403
        headers = {'X-Meshcore-Client': 'web'}
        assert client.post('/api/messages', headers=headers, json={'kind': 'channel', 'target': '0', 'text': 'Moin'}).status_code == 409
        assert client.post('/api/messages', headers=headers, json={'kind': 'bad', 'target': '0', 'text': 'Moin'}).status_code == 422
        assert client.options('/api/messages', headers={'Origin': 'https://example.com', 'Access-Control-Request-Headers': 'X-Meshcore-Client'}).headers.get('access-control-allow-origin') is None


def test_default_scope_frame_unicode_and_clear(bridge):
    import hashlib
    async def scenario():
        bridge.scope_supported = True
        bridge.radio.commands.get_default_flood_scope.return_value = Event(EventType.DEFAULT_FLOOD_SCOPE, {'scope_name': '#küste'})
        await bridge.save_scope(ScopeInput(scope='küste'))
        frame = bridge.radio.commands.send.call_args.args[0]
        name = '#küste'.encode()
        assert frame == b'\x3f' + name.ljust(31, b'\0') + hashlib.sha256(name).digest()[:16]
        assert bridge.snapshot()['scopes']['default'] == '#küste'
        bridge.radio.commands.get_default_flood_scope.return_value = Event(EventType.DEFAULT_FLOOD_SCOPE, {})
        await bridge.save_scope(ScopeInput(scope=''))
        assert bridge.radio.commands.send.call_args.args[0] == b'\x3f'
        assert bridge.default_scope == ''
    asyncio.run(scenario())


@pytest.mark.parametrize('scope', ['#', 'a b', 'a\x00b', 'ä' * 15, 'a' * 30])
def test_invalid_scope_rejected(scope):
    with pytest.raises(HTTPException):
        normalize_scope(scope)


def test_channel_scope_is_persisted_and_reset_after_send(bridge):
    async def scenario():
        bridge.device = {'fw ver': 13}
        await bridge.save_scope(ScopeInput(channel='0', scope='bsmesh'))
        assert bridge.store.get_meta('channel_scopes', {}) == {'0': '#bsmesh'}
        bridge.radio.commands.set_flood_scope.assert_not_awaited()
        calls = []
        async def set_scope(scope):
            calls.append(('scope', scope))
            return Event(EventType.OK, {})
        async def send(*args):
            calls.append(('send', args[1]))
            return Event(EventType.OK, {})
        bridge.radio.commands.set_flood_scope.side_effect = set_scope
        bridge.radio.commands.send_chan_msg.side_effect = send
        await bridge.send('channel', '0', 'Moin')
        assert calls == [('scope', '#bsmesh'), ('send', 'Moin'), ('scope', '')]
    asyncio.run(scenario())


def test_failed_scope_prevents_transmission(bridge):
    bridge.device = {'fw ver': 13}
    bridge.channel_scopes = {'0': '#test'}
    bridge.radio.commands.set_flood_scope.side_effect = [Event(EventType.ERROR, {}), Event(EventType.OK, {})]
    with pytest.raises(RuntimeError):
        asyncio.run(bridge.send('channel', '0', 'Moin'))
    bridge.radio.commands.send_chan_msg.assert_not_awaited()
    assert bridge.radio.commands.set_flood_scope.call_args.args == ('',)


def test_failed_scope_reset_blocks_followup_sends(bridge):
    bridge.device = {'fw ver': 13}
    bridge.radio.commands.set_flood_scope.side_effect = [Event(EventType.OK, {}), Event(EventType.ERROR, {})]
    asyncio.run(bridge.send('channel', '0', 'Moin'))
    assert bridge.store.history('channel', '0')[0]['status'] == 'sent'
    assert not bridge.ready
    with pytest.raises(HTTPException):
        asyncio.run(bridge.send('dm', KEY, 'Moin'))


def test_failed_default_write_keeps_previous_value(bridge):
    bridge.scope_supported, bridge.default_scope = True, '#old'
    bridge.radio.commands.send.return_value = Event(EventType.ERROR, {})
    with pytest.raises(RuntimeError):
        asyncio.run(bridge.save_scope(ScopeInput(scope='new')))
    assert bridge.default_scope == '#old'
