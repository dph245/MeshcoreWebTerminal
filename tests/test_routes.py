"""Static routes: simulated Companion only; no network/radio traffic."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from meshcore import EventType
from meshcore.events import Event
from pydantic import ValidationError

from server.app import Bridge, RouteInput, Store

KEY = 'ab' * 32
RELAY = 'cd' * 32


@pytest.fixture
def routed():
    b = Bridge(Store(':memory:'), 'unused', 0)
    b.ready = True
    b.status = 'connected'
    contact = dict(public_key=KEY, type=1, adv_name='Target', flags=0, last_advert=0,
                   adv_lat=0, adv_lon=0, out_path='', out_path_len=-1, out_path_hash_mode=-1)
    device_contacts = {KEY: contact}
    b.contacts = deepcopy(device_contacts)
    b.contacts[RELAY] = dict(public_key=RELAY, type=2, adv_name='Relay')
    calls = []

    async def contacts():
        calls.append('contacts')
        return Event(EventType.CONTACTS, deepcopy(device_contacts))

    async def change(c, path, mode):
        calls.append('change')
        # Like the library, mutate the passed object before returning OK.
        c.update(out_path=path, out_path_len=len(path) // (2 * (mode + 1)), out_path_hash_mode=mode)
        device_contacts[c['public_key']] = deepcopy(c)
        await asyncio.sleep(0)
        return Event(EventType.OK, {})

    async def read(key):
        calls.append('read')
        return Event(EventType.NEXT_CONTACT, deepcopy(device_contacts[key.hex()]))

    async def send(*args):
        calls.append('send')
        return Event(EventType.MSG_SENT, dict(type=0, expected_ack=len(calls).to_bytes(4, 'big')))

    b.radio = SimpleNamespace(is_connected=True, commands=SimpleNamespace(
        get_contacts=AsyncMock(side_effect=contacts), change_contact_path=AsyncMock(side_effect=change),
        get_contact_by_key=AsyncMock(side_effect=read), send_msg=AsyncMock(side_effect=send),
        send_cmd=AsyncMock(side_effect=send), send_login=AsyncMock(side_effect=send),
        send_logout=AsyncMock(return_value=Event(EventType.OK, {})),
        reset_path=AsyncMock(return_value=Event(EventType.OK, {})),
        get_time=AsyncMock(return_value=Event(EventType.CURRENT_TIME, {'time': 1000})),
        send_device_query=AsyncMock(return_value=Event(EventType.DEVICE_INFO, {'path_hash_mode': 0}))))
    b.wait_dm_ack = AsyncMock()
    b.store.set_route(KEY, dict(path=['cd', '00', 'cd'], hash_size=1))
    yield b, device_contacts, calls
    b.store.db.close()


def test_manual_three_attempts_path_update_and_late_ack(routed):
    b, device, calls = routed
    async def wait(*args):
        device[KEY].update(out_path='ff', out_path_len=1)
        await b.on_event(Event(EventType.PATH_UPDATE, {'public_key': KEY}))
    b.wait_dm_ack.side_effect = wait
    async def scenario():
        result = await b.send('dm', KEY, 'hello')
        await b.pending_dms[result['id']]
        assert calls == ['contacts', 'change', 'read', 'send'] * 3
        b.radio.commands.reset_path.assert_not_awaited()
        row = b.store.history('dm', KEY[:12])[0]
        assert (row['status'], row['tx_mode'], row['tx_attempt']) == ('unconfirmed', 'MANUAL', 3)
        assert b.store.routes()[KEY]['path'] == ['cd', '00', 'cd']
        await b.on_event(Event(EventType.ACK, {'code': row['ack']}))
        assert b.store.delivered(result['id'])
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['readback', 'path_event', 'wrong_key', 'error', 'missing_api', 'prefix_collision', 'unsupported_size'])
def test_activation_failure_never_sends(routed, failure):
    b, device, calls = routed
    if failure == 'readback':
        b.radio.commands.change_contact_path.side_effect = None
        b.radio.commands.change_contact_path.return_value = Event(EventType.OK, {})
    elif failure == 'path_event':
        original = b.radio.commands.get_contact_by_key.side_effect
        async def read(key):
            result = await original(key)
            await b.on_event(Event(EventType.PATH_UPDATE, {'public_key': KEY}))
            return result
        b.radio.commands.get_contact_by_key.side_effect = read
    elif failure == 'wrong_key':
        b.radio.commands.get_contact_by_key.side_effect = None
        b.radio.commands.get_contact_by_key.return_value = Event(EventType.NEXT_CONTACT, {'public_key': RELAY})
    elif failure == 'error':
        b.radio.commands.change_contact_path.side_effect = TimeoutError
    elif failure == 'missing_api':
        del b.radio.commands.get_contact_by_key
    elif failure == 'prefix_collision':
        device[KEY[:12] + 'ef' * 26] = dict(device[KEY])
    else:
        b.store.set_route(KEY, dict(path=['cdef'], hash_size=2))
        b.radio.commands.send_device_query.return_value = Event(EventType.DEVICE_INFO, {})
    async def scenario():
        with pytest.raises(RuntimeError):
            await b.send('dm', KEY, 'hello')
        b.radio.commands.send_msg.assert_not_awaited()
        assert b.route_status[KEY]['state'] == 'failed'
        assert b.store.routes()[KEY]
    asyncio.run(scenario())


def test_ack_during_reactivation_stops_retry(routed):
    b, device, calls = routed
    original = b.radio.commands.get_contact_by_key.side_effect
    async def read(key):
        result = await original(key)
        if calls.count('read') == 2:
            row = b.store.history('dm', KEY[:12])[0]
            await b.on_event(Event(EventType.ACK, {'code': row['ack']}))
        return result
    b.radio.commands.get_contact_by_key.side_effect = read
    async def scenario():
        result = await b.send('dm', KEY, 'hello')
        await b.pending_dms[result['id']]
        assert b.store.delivered(result['id'])
        b.radio.commands.send_msg.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['cli_login', 'cli_command', 'room_login', 'room_message', 'logout'])
def test_remote_operations_use_verified_contact_route(routed, operation):
    b, device, calls = routed
    role = 3 if operation.startswith('room') else 2
    b.contacts[KEY]['type'] = device[KEY]['type'] = role
    async def scenario():
        if operation == 'cli_login':
            await b.repeater_action(KEY, 'login', 'secret')
        elif operation == 'cli_command':
            b.repeaters[KEY] = dict(status='logged_in', replies=[])
            await b.repeater_action(KEY, 'command', 'ver')
        elif operation == 'room_login':
            await b.login_room(KEY, '')
        elif operation == 'room_message':
            b.rooms[KEY] = dict(status='logged_in', can_post=True)
            result = await b.send('room', KEY, 'room post')
            await b.pending_dms[result['id']]
        else:
            await b.repeater_action(KEY, 'logout')
        assert calls == ([] if operation == 'logout' else ['contacts', 'change', 'read', 'send'] * (3 if operation == 'room_message' else 1))
        b.invalidate_rooms()
    asyncio.run(scenario())


def test_concurrent_sends_are_serialized_and_configuration_is_locked(routed):
    b, device, calls = routed
    gate = asyncio.Event()
    async def wait(*args):
        await gate.wait()
    b.wait_dm_ack.side_effect = wait
    async def scenario():
        first, second = await asyncio.gather(b.send('dm', KEY, 'first'), b.send('dm', KEY, 'second'))
        assert calls == ['contacts', 'change', 'read', 'send'] * 2
        with pytest.raises(HTTPException) as exc:
            await b.save_route(RouteInput(target=KEY, mode='AUTO'))
        assert exc.value.status_code == 409
        tasks = list(b.pending_dms.values())
        gate.set()
        await asyncio.gather(*tasks)
        assert calls == ['contacts', 'change', 'read', 'send'] * 6
        assert not b.dm_targets
        await b.save_route(RouteInput(target=KEY, mode='AUTO'))
        assert KEY not in b.store.routes()
    asyncio.run(scenario())


@pytest.mark.parametrize('size,count', [(1,63), (2,32), (3,21)])
def test_route_validation_boundaries_and_ambiguity(routed, size, count):
    b, device, calls = routed
    b.contacts['cd' * 31 + 'ef'] = dict(type=2)
    async def scenario():
        result = await b.save_route(RouteInput(target=KEY, mode='MANUAL', hash_size=size, path=[RELAY] * count))
        assert len(result['routes'][KEY]['path']) == count
        assert result['routes'][KEY]['warnings']
        if count < 63:
            with pytest.raises(HTTPException):
                await b.save_route(RouteInput(target=KEY, mode='MANUAL', hash_size=size, path=[RELAY] * (count + 1)))
        for path in ([], ['xy'], [KEY]):
            with pytest.raises(HTTPException):
                await b.save_route(RouteInput(target=KEY, mode='MANUAL', hash_size=size, path=path))
        assert calls == []  # saving configuration never transmits
    asyncio.run(scenario())
    with pytest.raises(ValidationError):
        RouteInput(target=KEY, mode='MANUAL', hash_size=4, path=['cdef1234'])


def test_route_restart_keeps_full_key_and_does_not_transmit(tmp_path):
    path = str(tmp_path / 'routes.sqlite')
    store = Store(path)
    store.set_route(KEY, dict(path=['cd', 'cd'], hash_size=1))
    mid = store.save('dm', KEY[:12], 'out', 'message', 1000, 'sending')
    store.record_attempt(mid, 'deadbeef', 'direct', 1)
    store.db.close()
    store = Store(path)
    b = Bridge(store, 'unused', 0)
    assert b.snapshot()['routes'][KEY]['path'] == ['cd', 'cd']
    assert not b.pending_dms and not b.route_status
    assert store.history('dm', KEY[:12])[0]['status'] == 'interrupted'
    asyncio.run(b.on_event(Event(EventType.ACK, {'code': 'deadbeef'})))
    assert store.delivered(mid)
    store.db.close()


@pytest.mark.parametrize('reported', [1, None])
def test_manual_unexpected_send_mode_stops(routed, reported):
    b, device, calls = routed
    b.radio.commands.send_msg.side_effect = None
    b.radio.commands.send_msg.return_value = Event(EventType.MSG_SENT, {'type': reported})
    async def scenario():
        with pytest.raises(RuntimeError):
            await b.send('dm', KEY, 'hello')
        assert not b.pending_dms
        assert b.route_status[KEY]['state'] == 'failed'
        b.radio.commands.reset_path.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('size,count', [(1,63), (2,32), (3,21)])
def test_library_encodes_direct_paths_without_trace_format(routed, size, count):
    from meshcore.commands.contact import ContactCommands
    b, device, calls = routed
    # Exercise the installed library's real encoder, intercept only the transport.
    wire = SimpleNamespace(send=AsyncMock(return_value=Event(EventType.OK, {})))
    path = ('00' + 'cd' * (size - 1)) * count
    asyncio.run(ContactCommands.update_contact(wire, deepcopy(device[KEY]), path, path_hash_mode=size - 1))
    frame = wire.send.call_args.args[0]
    assert frame[0] == 9
    assert frame[1:33] == bytes.fromhex(KEY)
    assert frame[35] == ((size - 1) << 6) | count
    assert frame[36:100] == bytes.fromhex(path).ljust(64, b'\0')


def test_cli_operations_cannot_interleave_activation(routed):
    b, device, calls = routed
    b.contacts[KEY]['type'] = device[KEY]['type'] = 2
    b.repeaters[KEY] = dict(status='logged_in', replies=[])
    async def scenario():
        await asyncio.gather(b.repeater_action(KEY, 'command', 'ver'), b.repeater_action(KEY, 'command', 'clock'))
        assert calls == ['contacts', 'change', 'read', 'send'] * 2
        await b.on_event(Event(EventType.CONTACT_MSG_RECV, {'txt_type': 1, 'pubkey_prefix': KEY[:12], 'text': 'reply'}))
        assert b.repeaters[KEY]['replies'][0]['text'] == 'reply'
        assert b.store.routes()[KEY]['path'] == ['cd', '00', 'cd']
    asyncio.run(scenario())


def test_route_api_validates_full_key_and_persists_offline(tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    database = str(tmp_path / 'api.sqlite')
    with TestClient(create_app(database, autoconnect=False)) as client:
        b = client.app.state.bridge
        b.contacts[KEY] = dict(type=1, public_key=KEY)
        b.store.set_meta('contacts', b.contacts)
        headers = {'X-Meshcore-Client': 'web'}
        body = dict(target=KEY.upper(), mode='MANUAL', hash_size=3, path=['0000cd'])
        assert client.post('/api/routes', json=body).status_code == 403
        assert client.post('/api/routes', headers=headers, json={**body, 'target': KEY[:12]}).status_code == 422
        response = client.post('/api/routes', headers=headers, json=body)
        assert response.status_code == 200
        assert response.json()['routes'][KEY]['path'] == ['0000cd']
        assert not b.radio
    with TestClient(create_app(database, autoconnect=False)) as client:
        assert client.get('/api/state').json()['routes'][KEY]['hash_size'] == 3
        result = client.post('/api/routes', headers=headers, json=dict(target=KEY, mode='AUTO'))
        assert result.status_code == 200 and not result.json()['routes']


def test_retry_activation_failure_preserves_message_and_stops(routed):
    b, device, calls = routed
    async def wait(*args):
        b.radio.commands.get_contact_by_key.side_effect = TimeoutError
    b.wait_dm_ack.side_effect = wait
    async def scenario():
        result = await b.send('dm', KEY, 'hello')
        await b.pending_dms[result['id']]
        b.radio.commands.send_msg.assert_awaited_once()
        b.radio.commands.reset_path.assert_not_awaited()
        row = b.store.history('dm', KEY[:12])[0]
        assert row['status'] == 'interrupted' and row['tx_mode'] == 'MANUAL'
        assert b.route_status[KEY]['state'] == 'failed'
    asyncio.run(scenario())


@pytest.mark.parametrize('manual', [False, True])
@pytest.mark.parametrize('early', [False, True])
def test_path_embedded_ack_via_companion_frames_stops_retries(routed, manual, early):
    """Firmware exposes a PATH with embedded ACK as 0x81 then 0x82.

    Exercise the real SDK decoder, including ACK before MSG_SENT returns.
    This simulates Companion frames, not radio decryption in the firmware.
    """
    from meshcore.reader import MessageReader
    b, device, calls = routed
    if not manual:
        b.store.set_route(KEY, None)
    configured = deepcopy(b.store.routes())
    reader = MessageReader(SimpleNamespace(dispatch=AsyncMock(side_effect=b.on_event)))

    async def receive(code):
        await reader.handle_rx(bytearray(b'\x81' + bytes.fromhex(KEY)))
        await reader.handle_rx(bytearray(b'\x82' + code + (250).to_bytes(4, 'little')))

    if early:
        original = b.radio.commands.send_msg.side_effect
        async def send(*args):
            result = await original(*args)
            await receive(result.payload['expected_ack'])
            return result
        b.radio.commands.send_msg.side_effect = send
    else:
        async def wait(mid, result):
            await receive(result.payload['expected_ack'])
        b.wait_dm_ack.side_effect = wait

    async def scenario():
        result = await b.send('dm', KEY, 'PATH ACK')
        if task := b.pending_dms.get(result['id']):
            await task
        assert b.store.delivered(result['id'])
        b.radio.commands.send_msg.assert_awaited_once()
        b.radio.commands.reset_path.assert_not_awaited()
        assert b.store.routes() == configured
        assert b.path_revision == 1
        assert [event['type'] for event in b.events if event['type'] in ('PATH_UPDATE', 'ACK')] == ['PATH_UPDATE', 'ACK']
    asyncio.run(scenario())
