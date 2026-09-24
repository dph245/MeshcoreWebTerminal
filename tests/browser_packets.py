"""Offline browser checks: all HTTP requests are intercepted; no radio traffic."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
fixtures = [
    {'payload_type': 5, 'route_type': 1, 'path_len': 2, 'path_hash_size': 1, 'path': 'abcd',
     'chan_name': '#test', 'message': '<img src=x onerror=alert(1)> Hallo Mesh', 'rssi': -103, 'snr': 4.5},
    {'payload_type': 5, 'route_type': 1, 'path_len': 0, 'chan_hash': 'aa'},
    {'payload_type': 4, 'route_type': 1, 'adv_name': 'Dach-Repeater', 'adv_type': 2,
     'adv_lat': 52.5, 'adv_lon': 13.4, 'path_len': 0},
    {'payload_type': 2, 'route_type': 2, 'path_len': 1, 'path_hash_size': 1, 'path': 'ab'},
    {'payload_type': 15, 'route_type': -1, 'path_len': 2, 'path': 'ff'},
]
state = {'status': 'connected', 'host': 'test', 'port': 5000, 'channels': [],
         'contacts': {'ab'+'0'*62: {'adv_name': 'Dach'}, 'cd'+'1'*62: {'adv_name': 'Ost'},
                      'cd'+'2'*62: {'adv_name': 'West'}},
         'events': [], 'info': {}, 'device': {}, 'stats': {}}
for index, payload in enumerate(fixtures):
    state['events'].append({'time': 1700000000+index, 'type': 'RX_LOG_DATA', 'payload': payload})
state['events'].append({'time': 1700000010, 'type': 'RAW_DATA', 'payload': {'payload': 'ff00', 'RSSI': -90}})

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    def route(request):
        path = request.request.url.split('http://mesh.test/')[1]
        if path.startswith('api/'):
            request.fulfill(json=state)
        else:
            request.fulfill(path=STATIC / (path or 'index.html'))
    page.route('**/*', route)
    page.add_init_script('''window.EventSource = class {
      constructor() {this.listeners={}; window.testStream=this;}
      addEventListener(name, callback) {this.listeners[name]=callback;}
    };''')
    page.goto('http://mesh.test/')
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    page.locator('#event-filter').select_option('radio')
    expect(page.locator('#events tr')).to_have_count(6)
    expect(page.locator('#events')).to_contain_text('Inhalt verschlüsselt')
    expect(page.locator('#events')).to_contain_text('Dach-Repeater meldet sich im Mesh')
    expect(page.locator('#events')).to_contain_text('Binäre Anwendungsdaten')
    page.locator('#events tr').filter(has_text='Hallo Mesh').get_by_role('button').click()
    expect(page.locator('#packet-message')).to_have_text(fixtures[0]['message'])
    expect(page.locator('#packet-fields')).to_contain_text('Dach (ab) → cd')
    expect(page.locator('#packet-fields')).to_contain_text('-103 dBm')
    expect(page.locator('#packet-fields')).to_contain_text('4.5 dB')
    assert page.locator('#packet-dialog img').count() == 0
    page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', state['events'][0])
    expect(page.locator('#packet-dialog')).to_be_visible()
    expect(page.locator('#packet-message')).to_have_text(fixtures[0]['message'])
    page.locator('#packet-dialog summary').click()
    expect(page.locator('#packet-raw')).to_contain_text('payload_type')
    for theme in ['dark-green', 'dark-blue', 'light']:
        page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('#packet-dialog').evaluate('(el)=>el.scrollWidth <= el.clientWidth')
        page.screenshot(path=f'/tmp/mesh-packet-{theme}.png')
    page.keyboard.press('Escape')
    expect(page.locator('#packet-dialog')).not_to_be_visible()
    page.locator('#events tr').filter(has_text='Direct ·').get_by_role('button').click()
    expect(page.locator('#packet-fields')).to_contain_text('Routing-Pfad')
    expect(page.locator('#packet-fields')).not_to_contain_text('Empfangspfad')
    assert not errors, errors
    browser.close()
print('Packet browser checks passed: readable metadata, encrypted/unknown/raw packets, aliases, XSS, live updates, themes and mobile dialog.')
