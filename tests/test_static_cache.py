"""Static asset versioning, without a Companion connection."""
import hashlib
import importlib
from pathlib import Path
import re
import shutil

from fastapi.testclient import TestClient

from server.app import create_app


def test_static_assets_are_versioned_and_revalidated(tmp_path, monkeypatch):
    module = importlib.import_module('server.app')
    static = tmp_path / 'static'
    shutil.copytree(Path(module.__file__).resolve().parent.parent / 'static', static)
    monkeypatch.setattr(module, '__file__', str(tmp_path / 'server' / 'app.py'))

    with TestClient(create_app(str(tmp_path / 'test.db'), autoconnect=False)) as client:
        index = client.get('/')
        assert index.status_code == 200
        assert index.headers['cache-control'] == 'no-cache'
        assert client.get('/index.html').text == index.text
        head = client.head('/')
        assert head.status_code == 200
        assert head.headers['cache-control'] == 'no-cache'
        assert not head.content
        assets = re.findall(r'(?:src|href)="(/[^"?]+)\?v=([0-9a-f]+)"', index.text)
        assert {name for name, _ in assets} == {
            '/theme.js', '/packets.js', '/cli-commands.js', '/app.js', '/style.css', '/favicon.svg'}
        for name, version in assets:
            content = (static / name.lstrip('/')).read_bytes()
            assert version == hashlib.sha256(content).hexdigest()[:16]
            asset = client.get(f'{name}?v={version}')
            assert asset.status_code == 200
            assert asset.content == content
            assert asset.headers['cache-control'] == 'no-cache'
            assert asset.headers['x-content-type-options'] == 'nosniff'
            cached = client.get(f'{name}?v={version}', headers={'If-None-Match': asset.headers['etag']})
            assert cached.status_code == 304
            assert cached.headers['cache-control'] == 'no-cache'
        assert client.get('/app.js').headers['cache-control'] == 'no-cache'

        # A content change automatically changes only that asset's URL.
        script = static / 'app.js'
        script.write_bytes(script.read_bytes() + b'\n// updated\n')
        updated = client.get('/')
        new_assets = dict(re.findall(r'(?:src|href)="(/[^"?]+)\?v=([0-9a-f]+)"', updated.text))
        previous = dict(assets)
        assert new_assets.pop('/app.js') != previous.pop('/app.js')
        assert new_assets == previous
