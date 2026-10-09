"""Manual TRACE: real library command shape, isolated from radio hardware."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from meshcore import EventType
from meshcore.events import Event

from server.app import Bridge, Store, TraceInput, TRACE_LIMITS, trace_path


@pytest.fixture
def bridge():
    b = Bridge(Store(':memory:'), 'test', 5000)
    b.ready = True
    b.status = 'connected'
    b.radio = SimpleNamespace(is_connected=True, commands=SimpleNamespace(
        send_trace=AsyncMock(return_value=Event(EventType.MSG_SENT, {'suggested_timeout': 2000}))))
    yield b
    b.interrupt_traces()
    b.store.db.close()


@pytest.mark.parametrize('size', [1, 2, 4, 8])
def test_limits_and_reverse(size):
    a, b, c = ['%02x' % i * size for i in [1, 2, 3]]
    setting = TraceInput(path=[a, b, c], hash_size=size, return_path=True)
    assert trace_path(setting, {}) == [a, b, c, b, a]
    assert len(trace_path(TraceInput(path=[a] * TRACE_LIMITS[size], hash_size=size), {})) == TRACE_LIMITS[size]
    with pytest.raises((HTTPException, ValueError)):
        trace_path(TraceInput(path=[a] * (TRACE_LIMITS[size] + 1), hash_size=size), {})
    with pytest.raises(HTTPException):
        trace_path(TraceInput(path=[a] * TRACE_LIMITS[size], hash_size=size, return_path=True), {})


@pytest.mark.parametrize('value', ['', 'a', 'zz', 'aa,bb', 'a a', 'abc', 'ab' * 32])
def test_invalid_ids(value):
    with pytest.raises(HTTPException):
        trace_path(TraceInput(path=[value]), {})


def test_contact_and_repeated_ids():
    key = 'ab' * 32
    assert trace_path(TraceInput(path=[key, 'ab', key]), {key: {'type': 2}}) == ['ab'] * 3
    with pytest.raises(HTTPException):
        trace_path(TraceInput(path=[key]), {key: {'type': 1}})


def reply(m, values=None):
    return dict(tag=m['tag'], auth=m['auth'], flags=m['flags'], path_len=len(m['path']),
                path=[{'hash': hop, 'snr': value} for hop, value in zip(m['path'], values or [-2.25, 0, 4.5])]
                     + [{'snr': 1.25}])


def test_once_correlated_duplicate_hops_and_distinct_runs(bridge):
    async def run():
        setting = TraceInput(path=['ab', 'cd'], return_path=True)
        first = (await bridge.trace(setting))['traces'][0]
        second = (await bridge.trace(setting))['traces'][0]
        assert first['id'] != second['id'] and first['tag'] != second['tag']
        bridge.radio.commands.send_trace.assert_any_await(0, first['tag'], 0, 'ab,cd,ab')
        payload = reply(first)
        await bridge.on_event(Event(EventType.TRACE_DATA, {**payload, 'tag': -1}))
        await bridge.on_event(Event(EventType.TRACE_DATA, {**payload, 'auth': 1}))
        await bridge.on_event(Event(EventType.TRACE_DATA, {**payload, 'flags': 1}))
        assert all(m['status'] == 'waiting' for m in bridge.store.traces())
        wrong = reply(first)
        wrong['path'][1]['hash'] = 'ff'
        bridge.receive_trace(wrong)
        assert all(m['status'] == 'waiting' for m in bridge.store.traces())
        await bridge.on_event(Event(EventType.TRACE_DATA, payload))
        m = next(m for m in bridge.store.traces() if m['id'] == first['id'])
        assert m['snrs'] == [-2.25, 0, 4.5, 1.25]
        assert m['status'] == 'complete' and m['received_at'] >= m['timestamp']
        bridge.receive_trace(reply(first, [9, 9, 9]))
        assert next(m for m in bridge.store.traces() if m['id'] == first['id'])['snrs'] == m['snrs']
        bridge.expire_trace(second['id'])
        assert next(m for m in bridge.store.traces() if m['id'] == second['id'])['status'] == 'timeout'
        bridge.receive_trace(reply(second))  # Late reception still belongs to the original run.
        assert all(m['status'] == 'complete' for m in bridge.store.traces())
        assert bridge.radio.commands.send_trace.await_count == 2
    asyncio.run(run())


def test_early_response_and_missing_values(bridge):
    async def send(*args):
        m = bridge.store.traces()[0]
        bridge.receive_trace(reply(m, [None, 0, -1]))
        return Event(EventType.MSG_SENT, {})
    bridge.radio.commands.send_trace.side_effect = send
    async def run():
        result = await bridge.trace(TraceInput(path=['ab', 'cd', 'ab']))
        assert result['traces'][0]['status'] == 'partial'
        assert result['traces'][0]['snrs'] == [None, 0, -1, 1.25]
        assert not bridge.trace_timers
    asyncio.run(run())


def test_failure_offline_and_invalid_never_retry(bridge):
    async def run():
        with pytest.raises(HTTPException):
            await bridge.trace(TraceInput(path=['bad']))
        bridge.ready = False
        with pytest.raises(HTTPException):
            await bridge.trace(TraceInput(path=['ab']))
        bridge.radio.commands.send_trace.assert_not_awaited()
        bridge.ready = True
        bridge.radio.commands.send_trace.return_value = Event(EventType.ERROR, {'reason': 'unsupported'})
        with pytest.raises(RuntimeError):
            await bridge.trace(TraceInput(path=['ab']))
        assert bridge.store.traces()[0]['status'] == 'unconfirmed'
        bridge.radio.commands.send_trace.assert_awaited_once()
    asyncio.run(run())


def test_disconnect_and_restart(tmp_path):
    path = str(tmp_path / 'trace.sqlite')
    store = Store(path)
    b = Bridge(store, 'test', 5000)
    b.ready = True
    b.radio = SimpleNamespace(is_connected=True, commands=SimpleNamespace(
        send_trace=AsyncMock(return_value=Event(EventType.MSG_SENT, {}))))
    async def run():
        await b.trace(TraceInput(path=['ab', 'ab']))
        await b.on_event(Event(EventType.DISCONNECTED, {}))
        assert not b.trace_timers
        assert store.traces()[0]['status'] == 'interrupted'
    asyncio.run(run())
    m = store.traces()[0]
    m['status'] = 'waiting'  # Simulate a crash before orderly shutdown.
    store.save_trace(m)
    store.db.close()
    store = Store(path)
    b = Bridge(store, 'test', 5000)
    assert b.snapshot()['traces'][0]['path'] == ['ab', 'ab']
    assert b.snapshot()['traces'][0]['id'] == m['id']
    assert b.snapshot()['traces'][0]['status'] == 'interrupted'
    assert b.radio is None
    store.db.close()


def test_http_validation_and_expiry(bridge):
    from fastapi.testclient import TestClient
    from server.app import create_app
    with TestClient(create_app(':memory:', autoconnect=False)) as client:
        b = client.app.state.bridge
        b.ready = True
        b.radio = bridge.radio
        b.radio.commands.send_trace.return_value = Event(EventType.MSG_SENT, {'suggested_timeout': 1})
        headers = {'X-Meshcore-Client': 'web'}
        assert client.post('/api/trace', json={'path': []}, headers=headers).status_code == 422
        assert client.post('/api/trace', json={'path': ['ab'], 'hash_size': 3}, headers=headers).status_code == 422
        b.radio.commands.send_trace.assert_not_awaited()
        response = client.post('/api/trace', json={'path': ['ab']}, headers=headers)
        assert response.status_code == 200
        assert response.json()['traces'][0]['status'] == 'waiting'
        client.portal.call(asyncio.sleep, 1.05)
        assert b.store.traces()[0]['status'] == 'timeout'
        assert b.store.traces()[0]['snrs'] == [None, None]
        b.radio.commands.send_trace.assert_awaited_once()
