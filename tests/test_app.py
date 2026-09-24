import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from meshcore import EventType
from meshcore.events import Event

from server.app import Bridge, Store, ScopeInput, create_app, public, normalize_scope, receive_path
from server.app import ChannelInput, ChannelRemoveInput, normalize_channel
from server.app import channel_echo_key, repeater_echo


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
