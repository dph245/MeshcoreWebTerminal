"""Route editor and diagnostics with intercepted HTTP; no Companion connection."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
KEY, RELAY, OTHER = 'ab' * 32, 'cd' * 32, 'ef' * 32
ROOM = '12' * 32
state = dict(status='connected', host='test', port=5000, channels=[dict(index=0, name='Public')], contacts={
    KEY: dict(type=1, adv_name='Chat'), RELAY: dict(type=2, adv_name='Relay'),
    OTHER: dict(type=1, adv_name='Anderer Kontakt'),
    ROOM: dict(type=3, adv_name='Roomserver'),
    'cd' * 31 + 'ef': dict(type=2, adv_name='Collision')}, events=[], info={}, device={}, stats={}, routes={})
requests = []
messages = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    def route(r):
        path = r.request.url.split('http://mesh.test/')[1]
        if r.request.method == 'POST':
            assert path == 'api/routes'
            body = r.request.post_data_json
            requests.append(body)
            if body['mode'] == 'AUTO':
                state['routes'].pop(body['target'], None)
                state['contacts'][body['target']].update(out_path_len=-1, out_path='')
            else:
                size = body['hash_size']
                state['routes'][body['target']] = dict(hash_size=size, path=[hop[:size*2] for hop in body['path']])
            r.fulfill(json=state)
        elif path.startswith('api/messages'):
            r.fulfill(json=messages)
        elif path.startswith('api/'):
            r.fulfill(json=state)
        else:
            r.fulfill(path=STATIC / (path or 'index.html'))
    page.route('**/*', route)
    page.add_init_script('''window.EventSource=class {
      constructor(){this.listeners={};window.testStream=this;}
      addEventListener(name,callback){this.listeners[name]=callback;}
    };''')
    page.goto('http://mesh.test/')
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    page.locator('#channels button').click()
    expect(page.locator('#chat-route-edit')).to_be_hidden()
    expect(page.locator('#chat-route-summary')).to_be_hidden()
    page.locator('#contacts button').filter(has_text='Chat').click()
    expect(page.locator('.chat-heading #chat-route-edit')).to_have_text('Routing · AUTO')
    summary = page.locator('.chat-heading #chat-route-summary')
    expect(summary).to_have_text('AUTO · Pfad unbekannt')
    for fields, text in [
        (dict(out_path_len=-1, out_path=''), 'AUTO · FLOOD'),
        (dict(out_path_len=255), 'AUTO · FLOOD'),
        (dict(out_path_len=0), 'AUTO · DIRECT · direkt'),
        (dict(out_path_len=2, out_path='cd00', out_path_hash_mode=0), 'AUTO · DIRECT · cd → 00'),
        (dict(out_path_len=2, out_path='cdef0000', out_path_hash_mode=1), 'AUTO · DIRECT · cdef → 0000'),
        (dict(out_path_len=2, out_path='cdef12000000', out_path_hash_mode=2), 'AUTO · DIRECT · cdef12 → 000000'),
        (dict(out_path_len=2, out_path='bad'), 'AUTO · DIRECT · 2 Hops'),
    ]:
        state['contacts'][KEY].update(fields)
        page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
        expect(summary).to_have_text(text)
    state['route_status'] = {KEY: dict(state='stale')}
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(summary).to_have_text('AUTO · Pfad veraltet')
    state['route_status'] = {}
    page.locator('#message-text').fill('Entwurf bleibt')
    page.locator('#chat-route-edit').click()
    expect(page.locator('#route-title')).to_have_text('Route · Chat')
    page.locator('#route-mode').select_option('MANUAL')
    expect(page.locator('#route-save')).to_be_disabled()
    page.locator('#route-hops input').fill(RELAY)
    expect(page.locator('#route-validation')).to_contain_text('Warnung: mehrdeutige Hashes')
    page.locator('#route-add').click()
    page.locator('#route-hops input').nth(1).fill('00')
    page.locator('#route-add').click()
    page.locator('#route-hops input').nth(2).fill('cd')
    page.locator('#route-dialog').get_by_role('button', name='Hop 2 nach oben', exact=True).click()
    expect(page.locator('#route-preview')).to_have_text('00 → cd → cd')
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#route-preview')).to_have_text('00 → cd → cd')
    assert not requests
    page.locator('#route-save').click()
    expect(page.locator('#route-feedback')).to_contain_text('In SQLite gespeichert')
    assert requests[0]['path'] == ['00', RELAY, 'cd']
    assert requests[0]['target'] == KEY
    expect(page.locator('#route-current')).to_contain_text('Noch nicht aktiviert')
    page.locator('#route-close').click()
    expect(summary).to_have_text('MANUAL · 00 → cd → cd · noch nicht aktiviert')
    for status, text in [('confirmed', 'aktiviert'), ('stale', 'veraltet'), ('failed', 'Aktivierung fehlgeschlagen')]:
        state['route_status'] = {KEY: dict(state=status)}
        page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
        expect(summary).to_have_text(f'MANUAL · 00 → cd → cd · {text}')
    state['route_status'] = {}
    messages.append(dict(id=1, direction='out', text='Test', timestamp=1000, status='unconfirmed',
                         tx_mode='MANUAL', tx_route='direct', tx_attempt=3, tx_path=json.dumps(['00','cd','cd'])))
    page.locator('#terminal-nav').click()
    page.locator('#contacts button').filter(has_text='Chat').click()
    expect(page.locator('#chat-route-edit')).to_have_text('Routing · MANUAL')
    expect(page.locator('#chat-routing')).to_have_count(0)
    expect(page.locator('#message-text')).to_have_value('Entwurf bleibt')
    expect(page.locator('.message-meta')).to_contain_text('ACK-Timeout')
    expect(page.locator('.message-path summary')).to_have_text('MANUAL · DIRECT · Versuch 3')
    expect(page.locator('.message-path p')).to_be_hidden()
    page.locator('.message-path summary').click()
    expect(page.locator('.message-path p')).to_contain_text('Tatsächlich gesendete Hopfolge nicht übermittelt')
    page.locator('#contacts button').filter(has_text='Anderer Kontakt').click()
    expect(page.locator('#chat-route-edit')).to_have_text('Routing · AUTO')
    page.locator('#chat-route-edit').click()
    expect(page.locator('#route-title')).to_have_text('Route · Anderer Kontakt')
    expect(page.locator('#route-mode')).to_have_value('AUTO')
    page.locator('#route-close').click()
    assert OTHER not in state['routes']
    page.locator('#contacts button').filter(has_text='Chat').click()
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#chat-route-edit').click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    expect(page.locator('#route-preview')).to_have_text('00 → cd → cd')
    page.locator('#route-size').select_option('3')
    expect(page.locator('#route-save')).to_be_disabled()
    page.locator('#route-mode').select_option('AUTO')
    page.locator('#route-save').click()
    expect(page.locator('#route-current')).to_have_text('AUTO · FLOOD · kein gespeicherter Pfad')
    assert len(requests) == 2
    assert requests[1]['target'] == KEY
    page.locator('#route-close').click()
    expect(page.locator('#chat-route-edit')).to_have_text('Routing · AUTO')
    for width in [1280, 390]:
        page.set_viewport_size({'width':width,'height':844})
        if width<=700:page.locator('#conversation-back').click()
        page.locator('#rooms button').click()
        expect(page.locator('.chat-heading #chat-route-edit')).to_have_text('Routing · AUTO')
        page.locator('#message-text').fill('Room-Entwurf')
        page.locator('#chat-route-edit').click()
        expect(page.locator('#route-title')).to_have_text('Route · Roomserver')
        page.locator('#route-mode').select_option('MANUAL')
        page.locator('#route-size').select_option('1')
        page.locator('#route-hops input').fill('cd')
        page.locator('#route-save').click()
        expect(page.locator('#route-feedback')).to_contain_text('In SQLite gespeichert')
        assert requests[-1]['target'] == ROOM
        expect(page.locator('#route-current')).to_contain_text('Autonome Room-Synchronisation')
        page.locator('#route-close').click()
        expect(page.locator('#chat-route-edit')).to_have_text('Routing · MANUAL')
        expect(summary).to_be_visible()
        expect(summary).to_have_text('MANUAL · cd · noch nicht aktiviert')
        # Long routes stay compact; the complete path remains in the editor.
        state['routes'][ROOM]['path'] = ['abcdef'] * 21
        page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
        expect(summary).to_contain_text(' → '.join(['abcdef'] * 21))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        state['routes'][ROOM]['path'] = ['cd']
        page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
        expect(page.locator('#message-text')).to_have_value('Room-Entwurf')
        if width<=700:page.locator('#conversation-back').click()
        page.locator('#contacts button').filter(has_text='Chat').click()
        expect(page.locator('#chat-route-edit')).to_have_text('Routing · AUTO')
        if width<=700:page.locator('#conversation-back').click()
        page.locator('#rooms button').click()
        page.locator('#chat-route-edit').click()
        expect(page.locator('#route-preview')).to_have_text('cd')
        page.locator('#route-mode').select_option('AUTO')
        page.locator('#route-save').click()
        expect(page.locator('#route-feedback')).to_have_text('AUTO aktiv.')
        assert requests[-1]['target'] == ROOM
        page.locator('#route-close').click()
        expect(page.locator('#chat-route-edit')).to_have_text('Routing · AUTO')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    browser.close()
print('Static route browser checks passed')
