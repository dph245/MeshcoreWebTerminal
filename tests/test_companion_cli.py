"""Local Companion CLI, using simulated firmware only."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from meshcore import EventType
from meshcore.events import Event

from test_app import bridge


def test_companion_cli_reply_and_privacy(bridge):
    bridge.device = {'fw ver': 14}

    async def reply(command):
        assert command[3:] == 'get wifi.pwd'
        event = Event(EventType.CLI_REPLY, {'text': command[:3] + 'secret'})
        await bridge.on_event(event)
        return event

    bridge.radio.commands.run_cli_command = AsyncMock(side_effect=reply)
    assert asyncio.run(bridge.companion_command('get wifi.pwd')) == {'text': 'secret'}
    assert 'secret' not in str(bridge.snapshot())


@pytest.mark.parametrize('command', ['', ' ', 'get name\nreboot', 'a\x00b', 'a\x7fb', 'ä' * 81])
def test_companion_cli_rejects_invalid_commands(bridge, command):
    bridge.device = {'fw ver': 14}
    bridge.radio.commands.run_cli_command = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bridge.companion_command(command))
    assert exc.value.status_code == 422
    bridge.radio.commands.run_cli_command.assert_not_called()


@pytest.mark.parametrize(('ready', 'version'), [(False, 14), (True, 13)])
def test_companion_cli_requires_connection_and_firmware(bridge, ready, version):
    bridge.ready = ready
    bridge.device = {'fw ver': version}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bridge.companion_command('get name'))
    assert exc.value.status_code == 409


@pytest.mark.parametrize('result', [None, Event(EventType.ERROR, {'text': 'secret'}),
                                    Event(EventType.CLI_REPLY, {'text': 'old|secret'})])
def test_companion_cli_unconfirmed_response(bridge, result):
    bridge.device = {'fw ver': 14}
    bridge.radio.commands.run_cli_command = AsyncMock(return_value=result)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bridge.companion_command('get name'))
    assert exc.value.status_code == 502
    assert 'secret' not in exc.value.detail
    bridge.radio.commands.run_cli_command.assert_awaited_once()
