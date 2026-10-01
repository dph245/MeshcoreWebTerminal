import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from meshcore import EventType
from meshcore.events import Event

from server.app import Bridge, Store, ScopeInput, create_app, public, normalize_scope, receive_path
from server.app import ChannelInput, ChannelRemoveInput, normalize_channel
from server.app import channel_echo_key, repeater_echo


KEY = 'abcdef123456' + 'ab' * 26
ROOM = '123456abcdef' + 'cd' * 26


@pytest.fixture
def bridge():
    store = Store(':memory:')
    bridge = Bridge(store, '192.168.88.14', 5000)
    bridge.ready = True
    bridge.status = 'connected'
    bridge.channels = [{'index': 0, 'name': 'Public'}]
    bridge.contacts = {KEY: {'public_key': KEY, 'adv_name': 'Test', 'type': 1}}
    bridge.radio = SimpleNamespace(is_connected=True, commands=SimpleNamespace(
        send_appstart=AsyncMock(return_value=Event(EventType.SELF_INFO, {})),
        get_default_flood_scope=AsyncMock(return_value=Event(EventType.DEFAULT_FLOOD_SCOPE, {})),
        set_flood_scope=AsyncMock(return_value=Event(EventType.OK, {})),
        send=AsyncMock(return_value=Event(EventType.OK, {})),
        send_chan_msg=AsyncMock(return_value=Event(EventType.OK, {})),
        send_msg=AsyncMock(return_value=Event(EventType.MSG_SENT, {'expected_ack': bytes.fromhex('1234abcd')})),
    ))
    yield bridge
    store.db.close()


@pytest.fixture
def room_bridge(bridge):
    bridge.contacts[ROOM] = {'public_key': ROOM, 'adv_name': 'Testroom', 'type': 3}
    bridge.radio.commands.send_login = AsyncMock(return_value=Event(EventType.MSG_SENT, {'suggested_timeout': 10000}))
    bridge.radio.commands.send_logout = AsyncMock(return_value=Event(EventType.OK, {}))
    bridge.radio.commands.get_time = AsyncMock(return_value=Event(EventType.CURRENT_TIME, {'time': 1000}))
    return bridge


def test_room_login_requires_matching_confirmation(room_bridge):
    b = room_bridge
    async def scenario():
        snapshot = await b.login_room(ROOM, 'hello')
        assert snapshot['rooms'][ROOM]['status'] == 'logging_in'
        assert 'hello' not in str(snapshot)
        with pytest.raises(HTTPException):
            await b.login_room(ROOM, 'hello')
        await b.on_event(Event(EventType.LOGIN_SUCCESS, {'pubkey_prefix': KEY[:12], 'permissions': 0}))
        assert b.rooms[ROOM]['status'] == 'logging_in'
        await b.on_event(Event(EventType.LOGIN_SUCCESS, {'pubkey_prefix': ROOM[:12], 'permissions': 0, 'acl_permissions': 2}))
        assert b.rooms[ROOM]['can_post']
        assert b.rooms[ROOM]['status'] == 'logged_in'
        assert b.room_login_task is None
        assert not b.store.get_meta('rooms', {})
    asyncio.run(scenario())


@pytest.mark.parametrize(('permissions', 'acl', 'can_post'), [(2, None, False), (0, None, True), (1, None, True), (0, 0, False), (0, 1, False), (0, 2, True), (1, 3, True)])
def test_room_permissions_and_early_login(room_bridge, permissions, acl, can_post):
    b = room_bridge
    async def send_login(*args):
        payload = {'pubkey_prefix': ROOM[:12], 'permissions': permissions}
        if acl is not None:
            payload['acl_permissions'] = acl
        await b.on_event(Event(EventType.LOGIN_SUCCESS, payload))
        return Event(EventType.MSG_SENT, {})
    b.radio.commands.send_login.side_effect = send_login
    async def scenario():
        await b.login_room(ROOM, '')
        assert b.rooms[ROOM]['can_post'] is can_post
        assert b.room_login_task is None
    asyncio.run(scenario())


def test_room_timeout_and_late_login_do_not_enable_posting(room_bridge):
    b = room_bridge
    async def scenario():
        await b.login_room(ROOM, 'hello')
        b.room_login_task.cancel()
        await b.room_login_expiry(ROOM, 0)
        assert b.rooms[ROOM]['status'] == 'failed'
        await b.on_event(Event(EventType.LOGIN_SUCCESS, {'pubkey_prefix': ROOM[:12], 'permissions': 0}))
        assert not b.rooms[ROOM]['can_post']
    asyncio.run(scenario())


def test_room_login_errors_do_not_expose_password(room_bridge):
    b = room_bridge
    async def scenario():
        for password in ['ä' * 8, 'a\0b']:
            with pytest.raises(HTTPException) as error:
                await b.login_room(ROOM, password)
            assert error.value.status_code == 422
        b.radio.commands.send_login.assert_not_awaited()
        b.radio.commands.send_login.side_effect = RuntimeError('secret-pass')
        with pytest.raises(HTTPException) as error:
            await b.login_room(ROOM, 'secret-pass')
        assert 'secret-pass' not in str(error.value)
        assert 'secret-pass' not in str(b.snapshot())
    asyncio.run(scenario())


def test_room_rejected_login_and_disconnect(room_bridge):
    b = room_bridge
    async def scenario():
        await b.login_room(ROOM, 'hello')
        await b.on_event(Event(EventType.LOGIN_FAILED, {'pubkey_prefix': ROOM[:12]}))
        assert b.rooms[ROOM]['status'] == 'failed'
        assert b.room_login_task is None
        await b.login_room(ROOM, 'hello')
        await b.on_event(Event(EventType.DISCONNECTED, {}))
        assert b.rooms[ROOM]['status'] == 'disconnected'
        assert not b.rooms[ROOM]['can_post']
        assert b.room_login_task is None
    asyncio.run(scenario())


def test_room_posts_require_login_and_are_serialized(room_bridge):
    b = room_bridge
    async def scenario():
        with pytest.raises(HTTPException):
            await b.send('room', ROOM, 'Hello')
        with pytest.raises(HTTPException):
            await b.send('dm', ROOM, 'Hello')
        with pytest.raises(HTTPException):
            await b.login_room(KEY, 'hello')
        b.rooms[ROOM] = {'status': 'logged_in', 'can_post': True}
        with pytest.raises(HTTPException):
            await b.send('room', ROOM, 'ä' * 76)
        first = await b.send('room', ROOM, 'First')
        assert b.radio.commands.send_msg.call_args.args[2] == 1001
        assert b.snapshot()['rooms'][ROOM]['pending_send']
        with pytest.raises(HTTPException):
            await b.send('room', ROOM, 'Second')
        with pytest.raises(HTTPException):
            await b.login_room(ROOM, 'hello')
        task = b.pending_dms[first['id']]
        await b.on_event(Event(EventType.ACK, {'code': '1234abcd'}))
        await task
        await asyncio.sleep(0)  # Task completion releases the room slot.
        await b.send('room', ROOM, 'Second')
        assert b.radio.commands.send_msg.call_args.args[2] == 1002
        assert len(b.store.history('room', ROOM[:12])) == 2
        assert b.store.history('dm', ROOM[:12]) == []
        await b.stop_dms()
    asyncio.run(scenario())


def test_room_logout_stops_post_retries(room_bridge):
    b = room_bridge
    async def scenario():
        b.rooms[ROOM] = {'status': 'logged_in', 'can_post': True}
        await b.send('room', ROOM, 'Hello')
        await b.logout_room(ROOM)
        await b.stop_dms()
        assert not b.room_sends
        assert b.rooms[ROOM]['status'] == 'disconnected'
        assert b.store.history('room', ROOM[:12])[0]['status'] == 'interrupted'
        b.radio.commands.send_logout.assert_awaited_once_with(ROOM)
    asyncio.run(scenario())


def test_room_messages_keep_original_author_and_deduplicate(room_bridge):
    b = room_bridge
    async def scenario():
        payload = {'pubkey_prefix': ROOM[:12], 'txt_type': 2, 'signature': KEY[:8],
                   'sender_timestamp': 123, 'text': 'Same text', 'path_len': 255}
        for _ in range(2):
            await b.on_event(Event(EventType.CONTACT_MSG_RECV, payload))
        await b.on_event(Event(EventType.CONTACT_MSG_RECV, {**payload, 'signature': '99999999'}))
        rows = b.store.history('room', ROOM[:12])
        assert len(rows) == 2
        assert [r['sender_key'] for r in rows] == [KEY[:8], '99999999']
        assert rows[0]['reception']['routing'] == 'direct'
        assert b.store.history('dm', ROOM[:12]) == []
        del b.contacts[ROOM]
        await b.on_event(Event(EventType.CONTACT_MSG_RECV, {**payload, 'sender_timestamp': 124}))
        assert b.contacts[ROOM[:12]]['type'] == 3
        assert b.contacts[ROOM[:12]]['unknown']
    asyncio.run(scenario())


def test_room_api_validation_and_offline(tmp_path):
    with TestClient(create_app(str(tmp_path / 'rooms.db'), autoconnect=False)) as client:
        headers = {'X-Meshcore-Client': 'web'}
        assert client.post('/api/rooms/login', json={'target': ROOM, 'password': 'hello'}).status_code == 403
        assert client.post('/api/rooms/login', headers=headers, json={'target': ROOM, 'password': 'hello'}).status_code == 409
        assert client.post('/api/rooms/login', headers=headers, json={'target': KEY[:12]}).status_code == 422
        assert client.post('/api/rooms/logout', headers=headers, json={'target': ROOM}).status_code == 409
        assert client.get('/api/messages', params={'kind': 'room', 'target': ROOM}).json() == []


@pytest.mark.parametrize('version', [1, 3])
def test_room_signed_wire_message(room_bridge, version):
    from meshcore.reader import MessageReader
    async def scenario():
        dispatcher = SimpleNamespace(dispatch=AsyncMock(side_effect=room_bridge.on_event))
        reader = MessageReader(dispatcher)
        header = b'\x07' if version == 1 else b'\x10\x10\x00\x00'
        packet = header + bytes.fromhex(ROOM[:12]) + b'\xff\x02' + (123).to_bytes(4, 'little')
        await reader.handle_rx(bytearray(packet + bytes.fromhex(KEY[:8]) + b'Hello Room'))
        message = room_bridge.store.history('room', ROOM[:12])[0]
        assert message['sender_key'] == KEY[:8]
        assert message['text'] == 'Hello Room'
    asyncio.run(scenario())


def test_room_post_retries_and_ack(room_bridge):
    b = room_bridge
    configure_dm_retries(b, [0, 0, 0, 1, 1, 1])
    async def scenario():
        b.rooms[ROOM] = {'status': 'logged_in', 'can_post': True}
        result = await b.send('room', ROOM, 'Retry Room')
        await b.pending_dms[result['id']]
        await asyncio.sleep(0)
        assert b.radio.commands.send_msg.await_count == 6
        assert not b.room_sends
        assert b.store.history('room', ROOM[:12])[0]['status'] == 'unconfirmed'
        await b.on_event(Event(EventType.ACK, {'code': '00000002'}))
        assert b.store.history('room', ROOM[:12])[0]['status'] == 'delivered'
    asyncio.run(scenario())


def test_room_history_survives_restart(tmp_path):
    path = str(tmp_path / 'room_history.db')
    store = Store(path)
    store.save('room', ROOM[:12], 'in', 'Hello', 123, 'received', sender_key=KEY[:8])
    store.db.close()
    store = Store(path)
    assert store.history('room', ROOM[:12])[0]['sender_key'] == KEY[:8]
    assert store.save('room', ROOM[:12], 'in', 'Hello', 123, 'received', sender_key=KEY[:8]) is None
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
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'sending'
        await bridge.on_event(Event(EventType.ACK, {'code': '1234abcd'}))
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'delivered'
    asyncio.run(scenario())


def test_early_ack(bridge):
    async def scenario():
        await bridge.on_event(Event(EventType.ACK, {'code': '1234abcd'}))
        await bridge.send('dm', KEY, 'Frühes ACK')
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'delivered'
    asyncio.run(scenario())


def configure_dm_retries(bridge, routes):
    bridge.wait_dm_ack = AsyncMock()
    bridge.radio.commands.reset_path = AsyncMock(return_value=Event(EventType.OK, {}))
    bridge.radio.commands.send_msg.side_effect = [
        Event(EventType.MSG_SENT, {'type': route, 'expected_ack': i.to_bytes(4, 'big'), 'suggested_timeout': 1000})
        for i, route in enumerate(routes, 1)
    ]


@pytest.mark.parametrize('routes', [[0, 0, 0, 1, 1, 1], [1, 1, 1]])
def test_dm_retry_limits_and_late_ack(bridge, routes):
    configure_dm_retries(bridge, routes)
    async def scenario():
        result = await bridge.send('dm', KEY, 'Retry')
        await bridge.pending_dms[result['id']]
        calls = bridge.radio.commands.send_msg.call_args_list
        assert len(calls) == len(routes)
        assert len({call.args[2] for call in calls}) == 1  # Same timestamp.
        assert [call.args[3] for call in calls[1:]] == list(range(1, len(routes)))
        assert bridge.radio.commands.reset_path.await_count == (3 if routes[0] == 0 else 2)
        rows = bridge.store.history('dm', KEY[:12])
        assert len(rows) == 1
        assert rows[0]['status'] == 'unconfirmed'
        assert rows[0]['tx_route'] == 'flood'
        assert rows[0]['tx_attempt'] == 3
        assert not bridge.pending_dms
        await bridge.on_event(Event(EventType.ACK, {'code': '00000001'}))
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'delivered'
    asyncio.run(scenario())


@pytest.mark.parametrize('ack_wait', [1, 2, 3, 4, 6])
def test_ack_from_earlier_attempt_stops_retries(bridge, ack_wait):
    configure_dm_retries(bridge, [0, 0, 0, 1, 1, 1])
    waits = 0
    async def wait(mid, result):
        nonlocal waits
        waits += 1
        # Waiting must never hold the command lock, so polling and channels work.
        assert not bridge.lock.locked()
        if waits == ack_wait:
            await bridge.on_event(Event(EventType.ACK, {'code': '00000001'}))
    bridge.wait_dm_ack = wait
    async def scenario():
        result = await bridge.send('dm', KEY, 'Retry')
        await bridge.pending_dms[result['id']]
        assert bridge.radio.commands.send_msg.await_count == ack_wait
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'delivered'
    asyncio.run(scenario())


def test_ack_arriving_inside_retry_command(bridge):
    configure_dm_retries(bridge, [])
    count = 0
    async def send(*args):
        nonlocal count
        count += 1
        code = count.to_bytes(4, 'big')
        if count == 2:
            await bridge.on_event(Event(EventType.ACK, {'code': code.hex()}))
        return Event(EventType.MSG_SENT, {'type': 0, 'expected_ack': code})
    bridge.radio.commands.send_msg.side_effect = send
    async def scenario():
        result = await bridge.send('dm', KEY, 'Retry')
        await bridge.pending_dms[result['id']]
        assert count == 2
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'delivered'
    asyncio.run(scenario())


@pytest.mark.parametrize('started', [False, True])
def test_disconnect_cancels_pending_dm(bridge, started):
    async def scenario():
        await bridge.send('dm', KEY, 'Retry')
        if started:
            await asyncio.sleep(0)
        await bridge.on_event(Event(EventType.DISCONNECTED, {}))
        await bridge.stop_dms()
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'interrupted'
        bridge.radio.commands.send_msg.assert_awaited_once()
        assert not bridge.pending_dms
        assert not bridge.dm_wakeups
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['reset', 'send', 'connection'])
def test_dm_retry_errors_stop_transmissions(bridge, failure):
    configure_dm_retries(bridge, [0, 0, 0, 1, 1, 1])
    if failure == 'reset':
        bridge.radio.commands.reset_path.return_value = Event(EventType.ERROR, {})
    elif failure == 'send':
        bridge.radio.commands.send_msg.side_effect = [
            Event(EventType.MSG_SENT, {'type': 0, 'expected_ack': b'1234'}), TimeoutError()]
    async def scenario():
        result = await bridge.send('dm', KEY, 'Retry')
        if failure == 'connection':
            bridge.ready = False
        await bridge.pending_dms[result['id']]
        assert bridge.store.history('dm', KEY[:12])[0]['status'] == 'interrupted'
        assert bridge.radio.commands.send_msg.await_count == {'reset': 3, 'send': 2, 'connection': 1}[failure]
    asyncio.run(scenario())


def test_dm_restart_keeps_ack_mapping_and_stops_retries(tmp_path):
    path = str(tmp_path / 'dm.sqlite')
    store = Store(path)
    mid = store.save('dm', KEY[:12], 'out', 'Retry', 1, 'sent')
    store.record_attempt(mid, 'first', 'direct', 1)
    store.record_attempt(mid, 'second', 'direct', 2)
    store.db.close()
    store = Store(path)
    assert store.history('dm', KEY[:12])[0]['status'] == 'interrupted'
    store.acknowledge('first')
    assert store.history('dm', KEY[:12])[0]['status'] == 'delivered'
    store.db.close()


@pytest.mark.parametrize(('suggested', 'expected'), [(1000, 5.0), (30000, 36.0)])
def test_dm_ack_timeout_uses_companion_estimate(bridge, suggested, expected):
    async def scenario():
        mid = bridge.store.save('dm', KEY[:12], 'out', 'Wait', 1, 'sending')
        bridge.dm_wakeups[mid] = asyncio.Event()
        async def timeout(awaitable, seconds):
            assert seconds == expected
            awaitable.close()
            raise TimeoutError()
        with patch('server.app.asyncio.wait_for', side_effect=timeout):
            await bridge.wait_dm_ack(mid, Event(EventType.MSG_SENT, {'suggested_timeout': suggested}))
    asyncio.run(scenario())


def test_dm_ack_wakes_background_task(bridge):
    async def scenario():
        result = await bridge.send('dm', KEY, 'Wait')
        task = bridge.pending_dms[result['id']]
        await asyncio.sleep(0)
        # A channel transmission can proceed while the DM waits for an ACK.
        await bridge.send('channel', '0', 'Other message')
        await bridge.on_event(Event(EventType.ACK, {'code': '1234abcd'}))
        await asyncio.wait_for(task, .5)
        bridge.radio.commands.send_msg.assert_awaited_once()
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
    bridge.radio.commands.send_device_query = AsyncMock(return_value=Event(EventType.DEVICE_INFO, bridge.device))
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


@pytest.mark.parametrize(('payload', 'expected'), [
    ({'path_len': 2, 'path_hash_mode': 0, 'path': 'a1b2'}, ['a1', 'b2']),
    ({'path_len': 2, 'path_hash_mode': 1, 'path': 'a100b200'}, ['a100', 'b200']),
    ({'path_len': 2, 'path_hash_mode': 2, 'path': 'a10000b20000'}, ['a10000', 'b20000']),
    ({'path_len': 2, 'path_hash_mode': 2, 'path': 'a1b2'}, None),
    ({'path_len': 2, 'path_hash_mode': 0, 'path': 'zzzz'}, None),
    ({'path_len': 3}, None),
    ({'path_len': 0}, []),
])
def test_receive_path_hash_width_and_missing_data(payload, expected):
    assert receive_path(payload)['path'] == expected


def test_direct_routing_is_not_zero_hops():
    assert receive_path({'path_len': 255, 'path_hash_mode': -1}) == {'routing': 'direct', 'hops': None, 'path': None}
    assert receive_path({})['routing'] == 'unknown'


def test_old_database_migration_and_reception_persistence(tmp_path):
    import sqlite3
    path = str(tmp_path / 'old.db')
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, kind TEXT, target TEXT, direction TEXT, text TEXT, timestamp REAL, status TEXT, ack TEXT, fingerprint TEXT UNIQUE)')
    db.execute("INSERT INTO messages VALUES(1,'channel','0','in','Alt',123,'received',NULL,NULL)")
    db.commit()
    db.close()
    store = Store(path)
    assert store.history('channel', '0')[0]['reception'] is None
    reception = receive_path({'path_len': 1, 'path_hash_mode': 2, 'path': 'ab0012'})
    store.save('channel', '0', 'in', 'Neu', 124, 'received', reception=reception)
    store.db.close()
    store = Store(path)
    assert store.history('channel', '0')[1]['reception'] == reception
    store.db.close()


def test_channel_path_from_real_protocol_parser(bridge):
    from Crypto.Cipher import AES
    from Crypto.Hash import SHA256, HMAC
    from meshcore.reader import MessageReader
    async def scenario():
        dispatcher = SimpleNamespace(dispatch=AsyncMock())
        reader = MessageReader(dispatcher)
        reader.decrypt_channels = True
        secret = bytes(range(16))
        chan_hash = SHA256.new(secret).digest()[:1]
        await reader.packet_parser.newChannel({'channel_idx': 0, 'channel_name': 'Public', 'channel_hash': chan_hash.hex(), 'channel_secret': secret})
        timestamp = (123456).to_bytes(4, 'little')
        text = b'Test: Moin'
        plain = timestamp + b'\x00' + text
        cipher = AES.new(secret, AES.MODE_ECB).encrypt(plain.ljust(16, b'\x00'))
        mac = HMAC.new(secret, cipher, digestmod=SHA256).digest()[:2]
        # Flood group text, two 3-byte hop hashes, then encrypted payload.
        packet = b'\x15\x82' + bytes.fromhex('ab0012cd0034') + chan_hash + mac + cipher
        await reader.handle_rx(bytearray(b'\x88\x10\x9c' + packet))
        await reader.handle_rx(bytearray(b'\x11\x10\x00\x00\x00\x82\x00' + timestamp + text))
        event = dispatcher.dispatch.call_args.args[0]
        assert event.type == EventType.CHANNEL_MSG_RECV
        await bridge.on_event(event)
        reception = bridge.store.history('channel', '0')[0]['reception']
        assert reception == {'routing': 'flood', 'hops': 2, 'path': ['ab0012', 'cd0034']}
    asyncio.run(scenario())


def channel_radio(bridge):
    slots = {
        0: {'channel_name': 'Public', 'channel_secret': b'p' * 16},
        1: {'channel_name': '', 'channel_secret': bytes(16)},
    }
    bridge.device = {'max_channels': 2}
    async def get_channel(index):
        return Event(EventType.CHANNEL_INFO, dict(slots[index], channel_idx=index))
    async def set_channel(index, name, secret):
        slots[index] = {'channel_name': name, 'channel_secret': secret}
        return Event(EventType.OK, {})
    bridge.radio.commands.get_channel = AsyncMock(side_effect=get_channel)
    bridge.radio.commands.set_channel = AsyncMock(side_effect=set_channel)
    bridge.radio.commands.get_msg = AsyncMock(return_value=Event(EventType.NO_MORE_MSGS, {}))
    return slots


def test_hashtag_add_remove_and_slot_reuse(bridge):
    import hashlib
    async def scenario():
        slots = channel_radio(bridge)
        await bridge.change_channel(ChannelInput(name='küste'))
        assert slots[1] == {'channel_name': '#küste', 'channel_secret': hashlib.sha256('#küste'.encode()).digest()[:16]}
        assert slots[0]['channel_name'] == 'Public'
        bridge.store.save('channel', '1', 'in', 'Alter Verlauf', 123, 'received')
        bridge.channel_scopes['1'] = '#region'
        await bridge.change_channel(ChannelRemoveInput(index=1, name='#küste'), remove=True)
        assert slots[1] == {'channel_name': '', 'channel_secret': bytes(16)}
        assert '1' not in bridge.channel_scopes
        assert bridge.store.history('channel', '1') == []
        assert bridge.store.db.execute("SELECT count(*) FROM messages WHERE target LIKE 'archived:1:%'").fetchone()[0] == 1
        await bridge.change_channel(ChannelInput(name='#neu'))
        assert slots[1]['channel_name'] == '#neu'
        assert bridge.store.history('channel', '1') == []
    asyncio.run(scenario())


def test_channels_duplicate_full_and_stale_delete(bridge):
    async def scenario():
        channel_radio(bridge)
        await bridge.change_channel(ChannelInput(name='#test'))
        for setting, remove in [(ChannelInput(name='#test'), False), (ChannelInput(name='#other'), False),
                                (ChannelRemoveInput(index=1, name='#stale'), True)]:
            with pytest.raises(HTTPException) as exc:
                await bridge.change_channel(setting, remove)
            assert exc.value.status_code == 409
        assert bridge.radio.commands.set_channel.await_count == 1
    asyncio.run(scenario())


def test_channel_readback_mismatch_blocks_sends(bridge):
    channel_radio(bridge)
    bridge.radio.commands.set_channel.side_effect = None
    bridge.radio.commands.set_channel.return_value = Event(EventType.OK, {})
    with pytest.raises(RuntimeError):
        asyncio.run(bridge.change_channel(ChannelInput(name='#test')))
    assert not bridge.ready
    assert bridge.channels == [{'index': 0, 'name': 'Public'}]


@pytest.mark.parametrize('name', ['', '#', 'a b', 'a\x00b', 'a#b', 'ä' * 16])
def test_invalid_hashtag_name(name):
    with pytest.raises(HTTPException):
        normalize_channel(name)


def echo_event(timestamp, route_path='aa0001', **changes):
    payload = {'payload_type': 5, 'route_type': 1, 'chan_name': 'Public', 'chan_hash': 'ab',
               'sender_timestamp': timestamp, 'message': 'Mein Radio: Moin',
               'path_len': len(route_path)//6, 'path_hash_size': 3, 'path': route_path}
    payload.update(changes)
    return Event(EventType.RX_LOG_DATA, payload)


def test_outgoing_channel_echo_count_and_deduplication(bridge):
    async def scenario():
        bridge.info = {'name': 'Mein Radio'}
        bridge.channel_hashes = {'0': 'ab'}
        await bridge.send('channel', '0', 'Moin')
        message = bridge.store.history('channel', '0')[0]
        timestamp = int(message['timestamp'])
        assert message['repeater_count'] == 0
        for path in ['aa0001', 'aa0001', 'bb0002aa0001', 'bb0002', 'cc0003', 'dd0004']:
            await bridge.on_event(echo_event(timestamp, path))
        assert bridge.store.history('channel', '0')[0]['repeater_count'] == 4
        for changes in [{'message': 'Fremdes Radio: Moin'}, {'chan_name': '#anders'}, {'chan_hash': 'cd'},
                        {'sender_timestamp': timestamp+1}, {'route_type': 2}, {'payload_type': 4},
                        {'path_len': 0, 'path': ''}, {'path': 'nohex!'}]:
            await bridge.on_event(echo_event(timestamp, 'ee0005', **changes))
        assert bridge.store.history('channel', '0')[0]['repeater_count'] == 4
    asyncio.run(scenario())


def test_echo_before_send_response(bridge):
    async def scenario():
        bridge.info = {'name': 'Mein Radio'}
        bridge.channel_hashes = {'0': 'ab'}
        async def send(index, text, timestamp):
            await bridge.on_event(echo_event(timestamp))
            return Event(EventType.OK, {})
        bridge.radio.commands.send_chan_msg.side_effect = send
        await bridge.send('channel', '0', 'Moin')
        assert bridge.store.history('channel', '0')[0]['repeater_count'] == 1
    asyncio.run(scenario())


def test_repeater_count_survives_restart_and_ignores_ambiguous_transmission(tmp_path):
    path = str(tmp_path / 'echo.db')
    store = Store(path)
    key = channel_echo_key('#test', 'ab', 123, 'Radio: Moin')
    store.save('channel', '0', 'out', 'Moin', 123, 'sent', echo_key=key)
    assert store.record_repeater(key, 'ab1234')
    assert not store.record_repeater(key, 'ab')
    store.db.close()
    store = Store(path)
    assert store.history('channel', '0')[0]['repeater_count'] == 1
    store.save('channel', '0', 'out', 'Moin', 123, 'sent', echo_key=key)
    assert not store.record_repeater(key, 'cd5678')
    store.db.close()


def test_channel_sender_prefix_is_included_in_byte_limit(bridge):
    bridge.info = {'name': 'Mein Radio'}
    with pytest.raises(HTTPException):
        asyncio.run(bridge.send('channel', '0', 'x' * 149))
    bridge.radio.commands.send_chan_msg.assert_not_awaited()


@pytest.mark.parametrize('name,transport', [('#berlin', 'fa230000'), ('#küste', '947a0000')])
def test_received_scope_matches_packet_not_own_setting(bridge, name, transport):
    bridge.default_scope = '#hamburg'
    bridge.channel_scopes = {'0': name}
    payload = {'payload_type': 5, 'route_type': 0, 'transport_code': transport,
               'pkt_payload': 'aabbcc00112233445566778899'}
    bridge.log('RX_LOG_DATA', payload)
    result = bridge.events[-1]['payload']['received_scope']
    assert result['status'] == 'scoped'
    assert result['candidates'] == [name]
    assert 'received_scope' not in payload
    bridge.channel_scopes['0'] = '#other'
    assert bridge.events[-1]['payload']['received_scope'] == result


def test_received_scope_collision_and_unknown():
    from server.app import packet_scope
    payload = {'payload_type': 5, 'route_type': 0, 'transport_code': '9a470000',
               'pkt_payload': 'aabbcc00112233445566778899'}
    assert packet_scope(payload, ['#test186', '#test275'])['candidates'] == ['#test186', '#test275']
    assert packet_scope(payload, ['#hamburg']) == {'status': 'scoped', 'code': '0x479A', 'candidates': []}
    assert packet_scope({**payload, 'route_type': 1}, ['#test186']) == {'status': 'unscoped'}
    assert packet_scope({}, ['#test186']) == {'status': 'unknown'}
    assert packet_scope({**payload, 'transport_code': 'oops'}, []) == {'status': 'unknown'}
    for reserved in ('00000000', 'ffff0000'):
        assert packet_scope({**payload, 'transport_code': reserved}, [])['status'] == 'unknown'
    assert packet_scope({**payload, 'pkt_payload': 'f'}, ['#test186'])['candidates'] == []
    assert packet_scope({**payload, 'pkt_payload': 'zz'}, ['#test186'])['candidates'] == []
    assert packet_scope({**payload, 'pkt_payload': '00'}, ['#test186'])['candidates'] == []


@pytest.mark.parametrize('packet_first', [True, False])
@pytest.mark.parametrize('scoped', [True, False])
def test_chat_scope_correlates_both_event_orders(bridge, packet_first, scoped):
    async def scenario():
        bridge.channel_hashes = {'0': 'ab'}
        bridge.channel_names = {'0': 'Public'}
        bridge.default_scope = '#berlin'
        packet = echo_event(123, '', route_type=0 if scoped else 1,
                            transport_code='fa230000', pkt_payload='aabbcc00112233445566778899')
        message = Event(EventType.CHANNEL_MSG_RECV, {
            'channel_idx': 0, 'sender_timestamp': 123, 'text': 'Mein Radio: Moin', 'path_len': 0})
        for event in ([packet, message] if packet_first else [message, packet]):
            await bridge.on_event(event)
        item = bridge.store.history('channel', '0')[0]
        assert item['reception']['hops'] == 0
        scope = item['reception']['scope']
        assert scope['status'] == ('scoped' if scoped else 'unscoped')
        if scoped:
            assert scope['candidates'] == ['#berlin']
        # Duplicate inbox deliveries / relays must not overwrite captured metadata.
        bridge.default_scope = '#other'
        await bridge.on_event(message)
        await bridge.on_event(echo_event(123, '', route_type=1))
        assert bridge.store.history('channel', '0')[0]['reception']['scope'] == scope
    asyncio.run(scenario())


def test_chat_scope_never_matches_other_channel_text_or_time(bridge):
    async def scenario():
        bridge.channel_hashes = {'0': 'ab'}
        bridge.channel_names = {'0': 'Public'}
        await bridge.on_event(Event(EventType.CHANNEL_MSG_RECV, {
            'channel_idx': 0, 'sender_timestamp': 123, 'text': 'Mein Radio: Moin', 'path_len': 0}))
        for change in [{'chan_hash':'cd'}, {'chan_name':'Other'}, {'message':'Different'}, {'sender_timestamp':124}]:
            await bridge.on_event(echo_event(123, '', **change))
        assert 'scope' not in bridge.store.history('channel', '0')[0]['reception']
    asyncio.run(scenario())


def test_chat_scope_survives_restart(tmp_path):
    path = str(tmp_path / 'scopes.db')
    store = Store(path)
    store.save('channel', '0', 'in', 'Radio: Hallo', 123, 'received',
               reception={'routing':'flood','hops':1,'path':['ab']}, echo_key='packet-key')
    scope = {'status':'scoped','code':'0x1234','candidates':[]}
    assert store.record_received_scope('packet-key', scope)
    store.db.close()
    store = Store(path)
    assert store.history('channel', '0')[0]['reception']['scope'] == scope
    assert store.history('channel', '0')[0]['reception']['path'] == ['ab']
    store.db.close()


@pytest.mark.parametrize('size', [1, 2, 3])
def test_path_hash_saved_and_read_back(bridge, size):
    from server.app import PathHashInput
    bridge.radio.commands.send_device_query = AsyncMock(side_effect=[
        Event(EventType.DEVICE_INFO, {'path_hash_mode': 0}),
        Event(EventType.DEVICE_INFO, {'path_hash_mode': size - 1}),
    ])
    bridge.radio.commands.set_path_hash_mode = AsyncMock(return_value=Event(EventType.OK, {}))
    result = asyncio.run(bridge.save_path_hash(PathHashInput(bytes=size)))
    bridge.radio.commands.set_path_hash_mode.assert_awaited_once_with(size - 1)
    assert result['device']['path_hash_mode'] == size - 1
    assert bridge.ready


@pytest.mark.parametrize('enabled', [True, False])
def test_multi_acks_preserves_fresh_settings_and_reads_back(bridge, enabled):
    from server.app import MultiAcksInput
    info = {'multi_acks': int(not enabled), 'manual_add_contacts': True, 'adv_loc_policy': 2,
            'telemetry_mode_base': 1, 'telemetry_mode_loc': 2, 'telemetry_mode_env': 3}
    bridge.info = {**info, 'manual_add_contacts': False, 'telemetry_mode_loc': 0}
    bridge.radio.commands.send_device_query = AsyncMock(return_value=Event(EventType.DEVICE_INFO, {'fw ver': 7}))
    bridge.radio.commands.send_appstart.side_effect = [Event(EventType.SELF_INFO, info), Event(EventType.SELF_INFO, {**info, 'multi_acks': int(enabled)})]
    bridge.radio.commands.set_other_params_from_infos = AsyncMock(return_value=Event(EventType.OK, {}))
    result = asyncio.run(bridge.save_multi_acks(MultiAcksInput(enabled=enabled)))
    bridge.radio.commands.set_other_params_from_infos.assert_awaited_once_with({**info, 'multi_acks': int(enabled)})
    assert result['info']['multi_acks'] == int(enabled)
    assert bridge.ready


@pytest.mark.parametrize('failure', ['old_firmware', 'missing', 'mismatch', 'timeout', 'rejected'])
def test_multi_acks_unavailable_or_unconfirmed(bridge, failure):
    from server.app import MultiAcksInput
    info = {'multi_acks': 0, 'manual_add_contacts': False, 'adv_loc_policy': 0,
            'telemetry_mode_base': 0, 'telemetry_mode_loc': 0, 'telemetry_mode_env': 0}
    if failure == 'missing':
        del info['telemetry_mode_loc']
    bridge.radio.commands.send_device_query = AsyncMock(return_value=Event(EventType.DEVICE_INFO, {'fw ver': 6 if failure == 'old_firmware' else 7}))
    bridge.radio.commands.send_appstart.side_effect = [Event(EventType.SELF_INFO, info), TimeoutError() if failure == 'timeout' else Event(EventType.SELF_INFO, info)]
    bridge.radio.commands.set_other_params_from_infos = AsyncMock(return_value=Event(EventType.ERROR if failure == 'rejected' else EventType.OK, {}))
    with pytest.raises((HTTPException, RuntimeError, TimeoutError)):
        asyncio.run(bridge.save_multi_acks(MultiAcksInput(enabled=True)))
    if failure in ('old_firmware', 'missing'):
        bridge.radio.commands.set_other_params_from_infos.assert_not_awaited()
        assert bridge.ready
    else:
        assert not bridge.ready
        assert 'multi_acks' not in bridge.info


def test_multi_acks_api_validation_and_offline(tmp_path):
    with TestClient(create_app(str(tmp_path / 'acks.db'), autoconnect=False)) as client:
        headers = {'X-Meshcore-Client': 'web'}
        assert client.post('/api/multi-acks', json={'enabled': True}).status_code == 403
        for invalid in [0, 1, 'true', None]:
            assert client.post('/api/multi-acks', headers=headers, json={'enabled': invalid}).status_code == 422
        assert client.post('/api/multi-acks', headers=headers, json={'enabled': True}).status_code == 409


@pytest.mark.parametrize('failure', ['mismatch', 'timeout', 'rejected'])
def test_path_hash_unconfirmed_blocks_sends(bridge, failure):
    from server.app import PathHashInput
    bridge.radio.commands.send_device_query = AsyncMock(side_effect=[
        Event(EventType.DEVICE_INFO, {'path_hash_mode': 0}),
        TimeoutError() if failure == 'timeout' else Event(EventType.DEVICE_INFO, {'path_hash_mode': 0}),
    ])
    bridge.radio.commands.set_path_hash_mode = AsyncMock(return_value=Event(
        EventType.ERROR if failure == 'rejected' else EventType.OK, {}))
    with pytest.raises((RuntimeError, TimeoutError)):
        asyncio.run(bridge.save_path_hash(PathHashInput(bytes=3)))
    assert not bridge.ready
    assert bridge.status == 'reconnecting'
    assert 'path_hash_mode' not in bridge.device
    with pytest.raises(HTTPException):
        asyncio.run(bridge.send('channel', '0', 'Test'))
    bridge.radio.commands.send_chan_msg.assert_not_awaited()


def test_path_hash_unsupported(bridge):
    from server.app import PathHashInput
    bridge.radio.commands.send_device_query = AsyncMock(return_value=Event(EventType.DEVICE_INFO, {}))
    bridge.radio.commands.set_path_hash_mode = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bridge.save_path_hash(PathHashInput(bytes=2)))
    assert exc.value.status_code == 409
    bridge.radio.commands.set_path_hash_mode.assert_not_awaited()


def test_path_hash_api_validation_and_offline(tmp_path):
    with TestClient(create_app(str(tmp_path / 'path.db'), autoconnect=False)) as client:
        headers = {'X-Meshcore-Client': 'web'}
        assert client.post('/api/path-hash', json={'bytes': 2}).status_code == 403
        for invalid in [0, 4, -1, True, '2', 1.5, None]:
            assert client.post('/api/path-hash', json={'bytes': invalid}, headers=headers).status_code == 422
        assert client.post('/api/path-hash', json={'bytes': 2}, headers=headers).status_code == 409


def test_discovery_persists_and_combines_sources(tmp_path):
    path = str(tmp_path / 'discover.sqlite3')
    store = Store(path)
    b = Bridge(store, 'test', 5000)
    async def scenario():
        await b.on_event(Event(EventType.DISCOVER_RESPONSE, {'pubkey': KEY, 'node_type': 2, 'SNR': 5}))
        await b.on_event(Event(EventType.NEW_CONTACT, {'public_key': KEY, 'type': 2, 'adv_name': 'Relay'}))
        await b.on_event(Event(EventType.ADVERTISEMENT, {'public_key': KEY}))
        await b.on_event(Event(EventType.DISCOVER_RESPONSE, {'pubkey': 'broken', 'node_type': 2}))
    asyncio.run(scenario())
    assert not b.contacts
    assert len(b.discovered) == 1
    assert b.discovered[KEY]['sources'] == ['DISCOVER', 'ADVERT']
    assert b.discovered[KEY]['contact']['adv_name'] == 'Relay'
    assert b.discovered[KEY]['observations']['DISCOVER_RESPONSE']['payload']['SNR'] == 5
    store.db.close()
    store = Store(path)
    restored = Bridge(store, 'test', 5000)
    assert restored.snapshot()['discovered'][KEY]['contact']['type'] == 2
    store.db.close()


def test_raw_advert_is_saved_without_using_rx_path_as_tx_route(bridge):
    b = bridge
    asyncio.run(b.on_event(Event(EventType.RX_LOG_DATA, {
        'payload_type': 4, 'adv_key': ROOM, 'adv_type': 3, 'adv_name': 'Room',
        'path': 'abcd', 'path_len': 2, 'adv_lat': 50, 'adv_lon': 8})))
    contact = b.discovered[ROOM]['contact']
    assert contact['adv_name'] == 'Room'
    assert 'out_path' not in contact


def test_discover_requests_full_keys_and_checks_connection(bridge):
    b = bridge
    b.radio.commands.send_node_discover_req = AsyncMock(return_value=Event(EventType.OK, {}))
    asyncio.run(b.discover())
    b.radio.commands.send_node_discover_req.assert_awaited_once_with(0x1E, False)
    b.ready = False
    with pytest.raises(HTTPException):
        asyncio.run(b.discover())
    assert b.radio.commands.send_node_discover_req.await_count == 1


def test_save_discovery_confirms_contact_and_preserves_existing(bridge):
    b = bridge
    b.remember_discovery('DISCOVER_RESPONSE', {'pubkey': ROOM, 'node_type': 3})
    commands = b.radio.commands
    commands.get_contacts = AsyncMock(side_effect=[Event(EventType.CONTACTS, {}), Event(EventType.CONTACTS, {ROOM: {'type': 3}})])
    commands.add_contact = AsyncMock(return_value=Event(EventType.OK, {}))
    asyncio.run(b.save_discovered(ROOM))
    saved = commands.add_contact.call_args.args[0]
    assert saved['public_key'] == ROOM and saved['type'] == 3
    assert saved['out_path_len'] == -1
    assert ROOM in b.contacts
    commands.get_contacts = AsyncMock(return_value=Event(EventType.CONTACTS, {ROOM: {'flags': 7, 'adv_name': 'Custom'}}))
    asyncio.run(b.save_discovered(ROOM))
    assert commands.add_contact.await_count == 1
    assert b.contacts[ROOM]['adv_name'] == 'Custom'


@pytest.mark.parametrize('mode', ['prefix', 'missing_type', 'rejected', 'unconfirmed'])
def test_discovery_incomplete_and_failed_imports(bridge, mode):
    b = bridge
    key = ROOM[:16] if mode == 'prefix' else ROOM
    b.remember_discovery('DISCOVER_RESPONSE', {'pubkey': key, 'node_type': None if mode == 'missing_type' else 2})
    commands = b.radio.commands
    commands.get_contacts = AsyncMock(return_value=Event(EventType.CONTACTS, {}))
    commands.add_contact = AsyncMock(return_value=Event(EventType.ERROR if mode == 'rejected' else EventType.OK, {}))
    with pytest.raises((HTTPException, RuntimeError)):
        asyncio.run(b.save_discovered(key))
    assert key not in b.contacts
    assert key in b.discovered
    if mode in ('prefix', 'missing_type'):
        commands.add_contact.assert_not_awaited()


def test_manual_contact_add_and_duplicate_protection(bridge):
    from server.app import ContactInput
    b = bridge
    saved = {'public_key': ROOM, 'adv_name': 'Mein Room', 'type': 3}
    b.radio.commands.get_contacts = AsyncMock(side_effect=[Event(EventType.CONTACTS, {}), Event(EventType.CONTACTS, {ROOM: saved})])
    b.radio.commands.add_contact = AsyncMock(return_value=Event(EventType.OK, {}))
    setting = ContactInput(target=ROOM.upper(), name='  Mein Room  ', type=3)
    asyncio.run(b.change_contact(setting))
    sent = b.radio.commands.add_contact.call_args.args[0]
    assert sent['public_key'] == ROOM and sent['adv_name'] == 'Mein Room'
    assert sent['out_path_len'] == -1 and sent['flags'] == 0
    assert b.store.get_meta('contacts', {})[ROOM] == saved
    b.radio.commands.get_contacts = AsyncMock(return_value=Event(EventType.CONTACTS, {ROOM: saved}))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(b.change_contact(setting))
    assert exc.value.status_code == 409
    assert b.radio.commands.add_contact.await_count == 1


def test_remove_contact_preserves_history_and_discovery(bridge):
    from server.app import RoomTarget
    b = bridge
    b.store.save('dm', KEY[:12], 'in', 'Keep me', 123, 'received')
    b.remember_discovery('DISCOVER_RESPONSE', {'pubkey': KEY, 'node_type': 1})
    b.rooms[KEY] = {'status': 'logged_in', 'can_post': True}
    b.radio.commands.get_contacts = AsyncMock(side_effect=[Event(EventType.CONTACTS, b.contacts.copy()), Event(EventType.CONTACTS, {})])
    b.radio.commands.remove_contact = AsyncMock(return_value=Event(EventType.OK, {}))
    asyncio.run(b.change_contact(RoomTarget(target=KEY), remove=True))
    b.radio.commands.remove_contact.assert_awaited_once_with(KEY)
    assert not b.contacts and not b.store.get_meta('contacts', {})
    assert KEY not in b.rooms and KEY in b.discovered
    assert b.store.history('dm', KEY[:12])[0]['text'] == 'Keep me'


@pytest.mark.parametrize('busy', ['message', 'login'])
def test_remove_contact_rejects_active_operations(bridge, busy):
    from server.app import RoomTarget
    b = bridge
    if busy == 'message':
        b.store.save('dm', KEY[:12], 'out', 'Sending', 123, 'sending')
    else:
        b.rooms[KEY] = {'status': 'logging_in'}
    b.radio.commands.get_contacts = AsyncMock(return_value=Event(EventType.CONTACTS, b.contacts.copy()))
    b.radio.commands.remove_contact = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(b.change_contact(RoomTarget(target=KEY), remove=True))
    assert exc.value.status_code == 409
    b.radio.commands.remove_contact.assert_not_awaited()


@pytest.mark.parametrize('remove', [True, False])
@pytest.mark.parametrize('failure', ['rejected', 'readback'])
def test_contact_mutation_failure_is_not_reported_as_success(bridge, remove, failure):
    from server.app import ContactInput
    b = bridge
    before = b.contacts.copy() if remove else {}
    b.radio.commands.get_contacts = AsyncMock(return_value=Event(EventType.CONTACTS, before))
    mutation = AsyncMock(return_value=Event(EventType.ERROR if failure == 'rejected' else EventType.OK, {}))
    setattr(b.radio.commands, 'remove_contact' if remove else 'add_contact', mutation)
    with pytest.raises((HTTPException, RuntimeError)):
        asyncio.run(b.change_contact(ContactInput(target=KEY, name='Name', type=1), remove))
    assert b.contacts == before


def test_contact_api_validation_and_offline_guard(tmp_path):
    with TestClient(create_app(str(tmp_path / 'contacts.db'), autoconnect=False)) as client:
        headers = {'X-Meshcore-Client': 'web'}
        valid = {'target': KEY, 'name': 'Test', 'type': 1}
        assert client.post('/api/contacts', json=valid).status_code == 403
        for updates in ({'target': KEY[:12]}, {'target': 'z' * 64}, {'name': ' '},
                        {'name': 'a\x00b'}, {'name': 'ü' * 16}, {'type': 0}, {'type': True}):
            assert client.post('/api/contacts', json={**valid, **updates}, headers=headers).status_code == 422
        assert client.post('/api/contacts', json=valid, headers=headers).status_code == 409
        assert client.post('/api/contacts/remove', json={'target': KEY}, headers=headers).status_code == 409
        assert client.post('/api/contacts/remove', json={'target': KEY[:12]}, headers=headers).status_code == 422


def test_repeater_cli_login_reply_and_disconnect(bridge):
    b = bridge
    b.contacts[KEY]['type'] = 2
    b.radio.commands.send_login = AsyncMock(return_value=Event(EventType.MSG_SENT, {'suggested_timeout': 10000}))
    b.radio.commands.send_cmd = AsyncMock(return_value=Event(EventType.MSG_SENT, {}))
    b.radio.commands.send_logout = AsyncMock(return_value=Event(EventType.OK, {}))

    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await b.repeater_action(KEY, 'command', 'get name')
        assert exc.value.status_code == 409
        await b.repeater_action(KEY, 'login', 'secret')
        assert 'secret' not in str(b.snapshot())
        await b.on_event(Event(EventType.LOGIN_SUCCESS, {'pubkey_prefix': ROOM[:12]}))
        assert b.repeaters[KEY]['status'] == 'logging_in'
        await b.on_event(Event(EventType.LOGIN_SUCCESS, {'pubkey_prefix': KEY[:12]}))
        assert b.repeaters[KEY]['status'] == 'logged_in'
        assert not b.repeater_login_tasks
        await b.repeater_action(KEY, 'command', 'get name')
        b.radio.commands.send_cmd.assert_awaited_once_with(b.contacts[KEY], 'get name')
        await b.on_event(Event(EventType.CONTACT_MSG_RECV, {'pubkey_prefix': KEY[:12], 'txt_type': 1, 'text': '<Repeater>'}))
        assert b.repeaters[KEY]['replies'][0]['text'] == '<Repeater>'
        assert not b.store.history('dm', KEY[:12], None)
        assert all(e['type'] != 'CONTACT_MSG_RECV' for e in b.events)
        b.invalidate_rooms()
        assert b.repeaters[KEY]['status'] == 'disconnected'
        await b.repeater_action(KEY, 'logout')
        assert not b.repeaters[KEY]['replies']
    asyncio.run(scenario())


def test_repeater_cli_validation_and_failure(bridge):
    b = bridge
    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await b.repeater_action(KEY, 'login', '')
        assert exc.value.status_code == 404
        b.contacts[KEY]['type'] = 2
        for action, value in [('login', 'ä' * 8), ('login', '\x00'), ('command', 'get name\nreboot'), ('command', 'ä' * 81), ('command', ' ')]:
            with pytest.raises(HTTPException) as exc:
                await b.repeater_action(KEY, action, value)
            assert exc.value.status_code == 422
        b.radio.commands.send_login = AsyncMock(side_effect=RuntimeError('secret'))
        with pytest.raises(HTTPException) as exc:
            await b.repeater_action(KEY, 'login', 'secret')
        assert 'secret' not in str(exc.value)
        assert b.repeaters[KEY]['status'] == 'failed'
        b.repeaters[KEY]['status'] = 'logging_in'
        await b.repeater_login_expiry(KEY, 0)
        assert b.repeaters[KEY]['status'] == 'timeout'
        b.ready = False
        with pytest.raises(HTTPException) as exc:
            await b.repeater_action(KEY, 'login', '')
        assert exc.value.status_code == 409
    asyncio.run(scenario())
