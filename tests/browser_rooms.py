"""Offline Room UI checks; every request is intercepted, with no radio traffic."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
ROOM = '12' * 32
AUTHOR = 'ab' * 32
state = {'status': 'connected', 'host': 'test', 'port': 5000, 'channels': [],
         'contacts': {ROOM: {'type': 3, 'adv_name': 'Testroom'}, AUTHOR: {'type': 1, 'adv_name': 'Alice'}},
         'events': [], 'info': {'multi_acks': 0}, 'device': {'fw ver': 7}, 'stats': {}, 'rooms': {}}
messages = [{'id': 1, 'kind': 'room', 'target': ROOM[:12], 'direction': 'in',
             'text': '<img src=x> Hello', 'timestamp': 123, 'sender_key': AUTHOR[:8], 'status': 'received'}]
requests = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def route(r):
        path = r.request.url.split('http://mesh.test/')[1]
        if r.request.method == 'POST':
            data = r.request.post_data_json
            requests.append((path, data))
            if path == 'api/multi-acks':
                state['info']['multi_acks'] = int(data['enabled'])
            elif path == 'api/rooms/login':
                state['rooms'][ROOM] = {'status': 'logging_in', 'can_post': False}
            elif path == 'api/rooms/logout':
                state['rooms'][ROOM] = {'status': 'disconnected', 'can_post': False}
            else:
                assert path == 'api/messages' and data['kind'] == 'room'
                messages.append({'id': 2, 'direction': 'out', 'text': data['text'], 'timestamp': 124, 'status': 'delivered'})
            r.fulfill(json=state)
        elif path.startswith('api/messages'):
            assert 'kind=room' in path
            r.fulfill(json=messages)
        elif path.startswith('api/'):
            r.fulfill(json=state)
        else:
            r.fulfill(path=STATIC / (path or 'index.html'))

    page.route('**/*', route)
    page.add_init_script('''window.EventSource = class {
      constructor(){this.listeners={};window.testStream=this;}
      addEventListener(name,callback){this.listeners[name]=callback;}
    };''')
    page.goto('http://mesh.test/')

    def refresh():
        page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)

    refresh()
    expect(page.locator('#multi-acks-current')).to_have_text('Aus')
    page.locator('#multi-acks-enabled').check()
    refresh()  # A live update must preserve the user's unsaved choice.
    expect(page.locator('#multi-acks-enabled')).to_be_checked()
    page.locator('#multi-acks-save').click()
    expect(page.locator('#multi-acks-current')).to_have_text('Aktiv')
    expect(page.locator('#multi-acks-feedback')).to_contain_text('gespeichert und bestätigt')
    assert requests[-1] == ('api/multi-acks', {'enabled': True})
    page.locator('#multi-acks-enabled').uncheck()
    page.locator('#multi-acks-save').click()
    expect(page.locator('#multi-acks-current')).to_have_text('Aus')
    del state['info']['multi_acks']
    refresh()
    expect(page.locator('#multi-acks-save')).to_be_disabled()
    expect(page.locator('#multi-acks-current')).to_have_text('Nicht verfügbar')
    expect(page.locator('#rooms button')).to_have_count(1)
    expect(page.locator('#contacts button')).to_have_count(1)
    page.locator('#rooms button').click()
    expect(page.locator('#room-login-form')).to_be_visible()
    expect(page.locator('#messages')).to_contain_text('Alice · abababab')
    assert page.locator('#messages img').count() == 0
    page.locator('#message-text').fill('Hallo Room')
    expect(page.locator('#send')).to_be_disabled()
    page.locator('#room-password').fill('hello')
    page.locator('#room-login').click()
    expect(page.locator('#room-status')).to_have_text('Anmeldung läuft …')
    expect(page.locator('#room-password')).to_have_value('')
    assert requests[-1] == ('api/rooms/login', {'target': ROOM, 'password': 'hello'})
    state['rooms'][ROOM] = {'status': 'logged_in', 'can_post': False}
    refresh()
    expect(page.locator('#room-status')).to_contain_text('Nur lesen')
    expect(page.locator('#send')).to_be_disabled()
    state['rooms'][ROOM]['can_post'] = True
    refresh()
    expect(page.locator('#send')).to_be_enabled()
    page.locator('#message-text').fill('ä' * 76)
    expect(page.locator('#send')).to_be_disabled()
    page.locator('#message-text').fill('Hallo Room')
    page.locator('#send').click()
    expect(page.locator('#messages')).to_contain_text('Vom Roomserver angenommen ✓')
    state['rooms'][ROOM]['pending_send'] = True
    refresh()
    page.locator('#message-text').fill('Nächster Beitrag')
    expect(page.locator('#send')).to_be_disabled()
    expect(page.locator('#room-login')).to_be_disabled()
    page.locator('#room-logout').click()
    expect(page.locator('#room-status')).to_have_text('Nicht angemeldet')
    expect(page.locator('#send')).to_be_disabled()
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(page.locator('#rooms button')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    browser.close()
print('Room and ACK settings UI checks passed (desktop and mobile, simulated API).')
