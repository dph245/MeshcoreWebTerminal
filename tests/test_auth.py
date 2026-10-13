"""Shared authentication, using a simulated Companion and virtual waits only."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from meshcore import EventType
from meshcore.events import Event

from server.app import RouteInput
from test_routes import routed, KEY, RELAY


@pytest.fixture(params=['repeater', 'room', 'room_cli'])
def auth_bridge(routed, request):
    b, device, calls = routed
    role = request.param
    device[KEY]['type'] = b.contacts[KEY]['type'] = 2 if role == 'repeater' else 3
    b.store.set_route(KEY, None)
    b.wait_login = AsyncMock()

    async def send(*args):
        calls.append('send')
        return Event(EventType.MSG_SENT, {'type': 1 if device[KEY]['out_path_len'] == -1 else 0,
                                          'suggested_timeout': 40000})
    b.radio.commands.send_login.side_effect = send

    async def start():
        if role == 'room':
            await b.login_room(KEY, 'secret')
        else:
            await b.repeater_action(KEY, 'login', 'secret')
        return b.auth

    def session():
        return (b.rooms if role == 'room' else b.repeaters)[KEY]

    return b, device, calls, start, session


@pytest.mark.parametrize('outcome', [EventType.LOGIN_SUCCESS, EventType.LOGIN_FAILED])
@pytest.mark.parametrize('early', [True, False])
def test_reply_stops_without_retry(auth_bridge, outcome, early):
    b, device, calls, start, session = auth_bridge
    reply = Event(outcome, {'pubkey_prefix': KEY[:12], 'acl_permissions': 2})
    if early:
        original = b.radio.commands.send_login.side_effect
        async def send(*args):
            await b.on_event(reply)
            return await original(*args)
        b.radio.commands.send_login.side_effect = send
    else:
        b.wait_login.side_effect = lambda *args: None
        async def wait(*args):
            await b.on_event(reply)
        b.wait_login.side_effect = wait
    async def scenario():
        auth = await start()
        if auth:
            await auth['task']
        assert session()['status'] == ('logged_in' if outcome == EventType.LOGIN_SUCCESS else 'failed')
        b.radio.commands.send_login.assert_awaited_once()
        b.radio.commands.reset_path.assert_not_awaited()
        assert 'secret' not in str(b.snapshot())
    asyncio.run(scenario())


def test_manual_three_timeouts_and_terminal_late_reply(auth_bridge):
    b, device, calls, start, session = auth_bridge
    b.store.set_route(KEY, {'path': ['cd'], 'hash_size': 1})
    async def scenario():
        auth = await start()
        await auth['task']
        assert calls.count('send') == 3
        assert b.wait_login.await_count == 3
        assert all(call.args[1] == 60 for call in b.wait_login.await_args_list)
        b.radio.commands.reset_path.assert_not_awaited()
        assert session()['status'] in ('failed', 'timeout')
        await b.on_event(Event(EventType.LOGIN_SUCCESS, {'pubkey_prefix': KEY[:12], 'acl_permissions': 2}))
        assert session()['status'] in ('failed', 'timeout')
        assert not session().get('can_post')
        assert b.auth is None
    asyncio.run(scenario())


def test_auto_stale_direct_falls_back_once(auth_bridge):
    b, device, calls, start, session = auth_bridge
    device[KEY].update(out_path='cd', out_path_len=1, out_path_hash_mode=0)
    async def scenario():
        auth = await start()
        await auth['task']
        assert calls.count('send') == 2
        b.radio.commands.reset_path.assert_awaited_once_with(KEY)
        events = [e['payload'] for e in b.events if e['type'] == 'AUTH_LOGIN']
        assert [e['route'] for e in events if e['result'] == 'sent'] == ['direct', 'flood']
        assert events[-1]['result'] == 'timeout'
        assert b.contacts[KEY]['out_path_len'] == -1
    asyncio.run(scenario())


def test_unknown_path_only_one_flood(auth_bridge):
    b, device, calls, start, session = auth_bridge
    async def scenario():
        auth = await start()
        await auth['task']
        assert calls.count('send') == 1
        b.radio.commands.reset_path.assert_not_awaited()
    asyncio.run(scenario())


def test_learned_paths_survive_timeouts(auth_bridge):
    b, device, calls, start, session = auth_bridge
    async def wait(*args):
        device[KEY].update(out_path=f'{calls.count("send"):02x}', out_path_len=1, out_path_hash_mode=0)
        await b.on_event(Event(EventType.PATH_UPDATE, {'public_key': KEY}))
    b.wait_login.side_effect = wait
    async def scenario():
        auth = await start()
        await auth['task']
        assert calls.count('send') == 3
        b.radio.commands.reset_path.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('where', ['waiting_lock', 'read', 'reset', 'second_send'])
def test_late_reply_prevents_next_possible_send(auth_bridge, where):
    b, device, calls, start, session = auth_bridge
    device[KEY].update(out_path='cd', out_path_len=1, out_path_hash_mode=0)
    reply = Event(EventType.LOGIN_SUCCESS, {'pubkey_prefix': KEY[:12], 'acl_permissions': 2})
    original_read = b.radio.commands.get_contact_by_key.side_effect
    original_reset = b.radio.commands.reset_path.side_effect
    original_send = b.radio.commands.send_login.side_effect
    async def read(key):
        result = await original_read(key)
        if where == 'read' and calls.count('send') == 1:
            await b.on_event(reply)
        return result
    async def reset(key):
        result = await original_reset(key)
        if where == 'reset':
            await b.on_event(reply)
        return result
    async def send(*args):
        result = await original_send(*args)
        if where == 'second_send' and calls.count('send') == 2:
            await b.on_event(reply)
        return result
    b.radio.commands.get_contact_by_key.side_effect = read
    b.radio.commands.reset_path.side_effect = reset
    b.radio.commands.send_login.side_effect = send
    async def scenario():
        auth = await start()
        if where == 'waiting_lock':
            async with b.lock:
                await asyncio.sleep(0)
                await b.on_event(reply)
        await auth['task']
        assert session()['status'] == 'logged_in'
        assert calls.count('send') == (2 if where == 'second_send' else 1)
    asyncio.run(scenario())


def test_overlap_route_changes_and_disconnect(auth_bridge):
    b, device, calls, start, session = auth_bridge
    async def scenario():
        auth = await start()
        with pytest.raises(HTTPException) as exc:
            await start()
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException):
            await b.save_route(RouteInput(target=KEY, mode='AUTO'))
        b.contacts[RELAY]['type'] = 3
        with pytest.raises(HTTPException):
            await b.login_room(RELAY, '')
        with pytest.raises(HTTPException):
            await b.repeater_action(RELAY, 'login', '')
        await b.on_event(Event(EventType.DISCONNECTED, {}))
        await asyncio.gather(auth['task'], return_exceptions=True)
        assert b.auth is None
        assert session()['status'] == 'disconnected'
        assert calls.count('send') == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['send_timeout', 'send_error', 'reset_error', 'reset_readback', 'retry_error'])
def test_failures_stop_and_hide_credentials(auth_bridge, failure):
    b, device, calls, start, session = auth_bridge
    device[KEY].update(out_path='cd', out_path_len=1, out_path_hash_mode=0)
    if failure.startswith('send_'):
        b.radio.commands.send_login.side_effect = TimeoutError('secret') if failure == 'send_timeout' else RuntimeError('secret')
    elif failure == 'reset_error':
        b.radio.commands.reset_path.side_effect = RuntimeError('secret')
    elif failure == 'reset_readback':
        b.radio.commands.reset_path.side_effect = None
        b.radio.commands.reset_path.return_value = Event(EventType.OK, {})
    else:
        original = b.radio.commands.send_login.side_effect
        async def send(*args):
            if calls.count('send'):
                raise RuntimeError('secret')
            return await original(*args)
        b.radio.commands.send_login.side_effect = send
    async def scenario():
        if failure.startswith('send_'):
            with pytest.raises(HTTPException) as exc:
                await start()
            assert 'secret' not in str(exc.value)
        else:
            auth = await start()
            await auth['task']
        assert session()['status'] == 'failed'
        assert b.auth is None
        assert 'secret' not in str(b.snapshot())
        assert calls.count('send') <= 1
    asyncio.run(scenario())


@pytest.mark.parametrize('hint,expected', [(None,60), (-1,60), ('bad',60), (float('nan'),60), (0,60), (1000,30), (120000,180), (999999,300)])
def test_timeout_bounds(routed, hint, expected):
    b, _, _ = routed
    assert b.login_timeout(Event(EventType.MSG_SENT, {'suggested_timeout': hint})) == expected


def test_auto_transition_resets_once_and_preserves_learned_path(routed):
    b, device, calls = routed
    async def scenario():
        await b.activate_route(KEY, b.store.routes()[KEY])
        await b.save_route(RouteInput(target=KEY, mode='AUTO'))
        assert KEY not in b.store.routes()
        assert b.contacts[KEY]['out_path_len'] == -1
        b.radio.commands.reset_path.assert_awaited_once()
        device[KEY].update(out_path='ef', out_path_len=1, out_path_hash_mode=0)
        await b.save_route(RouteInput(target=KEY, mode='AUTO'))
        await b.command('send_login', KEY, '')
        await b.command('send_login', KEY, '')
        b.radio.commands.reset_path.assert_awaited_once()
        assert device[KEY]['out_path'] == 'ef'
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['offline', 'reset', 'readback'])
def test_failed_auto_transition_keeps_manual_configuration(routed, failure):
    b, device, calls = routed
    async def scenario():
        await b.activate_route(KEY, b.store.routes()[KEY])
        if failure == 'offline':
            b.ready = False
        elif failure == 'reset':
            b.radio.commands.reset_path.side_effect = TimeoutError
        else:
            b.radio.commands.reset_path.side_effect = None
        with pytest.raises((HTTPException, RuntimeError)):
            await b.save_route(RouteInput(target=KEY, mode='AUTO'))
        assert b.store.routes()[KEY]
        assert not calls.count('send')
    asyncio.run(scenario())


@pytest.mark.parametrize('success', [True, False])
def test_sdk_login_push_frames(auth_bridge, success):
    from types import SimpleNamespace
    from meshcore.reader import MessageReader
    b, device, calls, start, session = auth_bridge
    reader = MessageReader(SimpleNamespace(dispatch=AsyncMock(side_effect=b.on_event)))
    async def wait(*args):
        frame = bytes([0x85 if success else 0x86, 0]) + bytes.fromhex(KEY[:12])
        if success:
            frame += (1234).to_bytes(4, 'little') + bytes([2, 1])
        await reader.handle_rx(bytearray(frame))
    b.wait_login.side_effect = wait
    async def scenario():
        auth = await start()
        await auth['task']
        assert session()['status'] == ('logged_in' if success else 'failed')
        assert calls.count('send') == 1
    asyncio.run(scenario())


def test_reply_in_scheduled_send_window(auth_bridge):
    b, device, calls, start, session = auth_bridge
    original = b.raw_command
    async def command(method, *args, **kwargs):
        if method == 'send_login':
            # Scheduled between the caller's check and wait_for's invoke task.
            asyncio.create_task(b.on_event(Event(EventType.LOGIN_SUCCESS,
                                                  {'pubkey_prefix': KEY[:12], 'acl_permissions': 2})))
            await asyncio.sleep(0)
        return await original(method, *args, **kwargs)
    b.raw_command = command
    async def scenario():
        await start()
        assert session()['status'] == 'logged_in'
        assert calls.count('send') == 0
    asyncio.run(scenario())


def test_same_path_learned_again_is_not_immediately_reset(auth_bridge):
    b, device, calls, start, session = auth_bridge
    device[KEY].update(out_path='cd', out_path_len=1, out_path_hash_mode=0)
    async def wait(*args):
        await b.on_event(Event(EventType.PATH_UPDATE, {'public_key': KEY}))
    b.wait_login.side_effect = wait
    async def scenario():
        auth = await start()
        await auth['task']
        assert calls.count('send') == 3
        b.radio.commands.reset_path.assert_not_awaited()
    asyncio.run(scenario())


def test_reset_uses_protocol_opcode_without_radio():
    from types import SimpleNamespace
    from meshcore.commands.contact import ContactCommands
    wire = SimpleNamespace(send=AsyncMock(return_value=Event(EventType.OK, {})),
                           _get_contact_by_prefix=lambda key: None)
    asyncio.run(ContactCommands.reset_path(wire, KEY))
    assert wire.send.call_args.args[0] == bytes([0x0D]) + bytes.fromhex(KEY)
