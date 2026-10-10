"""Route editor and diagnostics with intercepted HTTP; no Companion connection."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
KEY, RELAY = 'ab' * 32, 'cd' * 32
state = dict(status='connected', host='test', port=5000, channels=[], contacts={
    KEY: dict(type=1, adv_name='Chat'), RELAY: dict(type=2, adv_name='Relay'),
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
    page.locator('#device-nav').click()
    page.locator('#contact-manager-nav').click()
    page.locator('#managed-contacts .managed-channel').filter(has_text='Chat').get_by_role('button', name='AUTO · Route').click()
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
    expect(page.locator('#route-current')).to_contain_text('Noch nicht aktiviert')
    page.locator('#route-close').click()
    messages.append(dict(id=1, direction='out', text='Test', timestamp=1000, status='unconfirmed',
                         tx_mode='MANUAL', tx_route='direct', tx_attempt=3, tx_path=json.dumps(['00','cd','cd'])))
    page.locator('#terminal-nav').click()
    page.locator('#contacts button').click()
    expect(page.locator('#chat-routing summary')).to_have_text('Routing · MANUAL')
    expect(page.locator('.message-meta')).to_contain_text('ACK-Timeout')
    expect(page.locator('.message-path summary')).to_have_text('MANUAL · DIRECT · Versuch 3')
    expect(page.locator('.message-path p')).to_be_hidden()
    page.locator('.message-path summary').click()
    expect(page.locator('.message-path p')).to_contain_text('Tatsächlich gesendete Hopfolge nicht übermittelt')
    page.locator('#device-nav').click()
    page.locator('#managed-contacts .managed-channel').filter(has_text='Chat').get_by_role('button', name='MANUAL · Route').click()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    expect(page.locator('#route-preview')).to_have_text('00 → cd → cd')
    page.locator('#route-size').select_option('3')
    expect(page.locator('#route-save')).to_be_disabled()
    page.locator('#route-mode').select_option('AUTO')
    page.locator('#route-save').click()
    expect(page.locator('#route-current')).to_have_text('AUTO · Automatische Routenerkennung')
    assert len(requests) == 2
    page.locator('#route-close').click()
    assert not errors, errors
    browser.close()
print('Static route browser checks passed')
