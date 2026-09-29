"""Discovery UI checks with intercepted requests; no radio traffic."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
KEY = 'ab' * 32
state = {'status': 'connected', 'host': 'test', 'port': 5000, 'channels': [],
         'contacts': {}, 'events': [], 'info': {}, 'device': {}, 'stats': {},
         'discovered': {KEY: {'contact': {'adv_name': '<Relay>', 'type': 2},
                             'sources': ['DISCOVER', 'ADVERT'], 'last_seen': 123,
                             'observations': {'DISCOVER_RESPONSE': {'time': 122, 'payload': {'SNR': -2.25, 'SNR_in': 0}}}}}}
requests = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def route(r):
        path = r.request.url.split('http://mesh.test/')[1]
        if r.request.method == 'POST':
            requests.append((path, r.request.post_data_json))
            if path == 'api/discovered/save':
                state['contacts'][KEY] = {'type': 2, 'adv_name': '<Relay>'}
            r.fulfill(json=state)
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
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#discovered-list')).to_contain_text('<Relay>')
    expect(page.locator('#discovered-list')).to_contain_text('DISCOVER + ADVERT')
    expect(page.locator('#discovered-list')).to_contain_text('Antwort bei dir: -2,25 dB')
    expect(page.locator('#discovered-list')).to_contain_text('Anfrage beim Gerät: 0 dB')
    page.locator('#discover').click()
    expect(page.locator('#discovery-feedback')).to_contain_text('DISCOVER gesendet')
    assert requests[-1] == ('api/discover', {})
    page.get_by_role('button', name='Als Kontakt speichern').click()
    expect(page.get_by_role('button', name='Im Kontaktbuch')).to_be_disabled()
    assert requests[-1] == ('api/discovered/save', {'target': KEY})
    page.locator('#discovered-search').fill('nonexistent')
    expect(page.locator('#discovered-list')).to_contain_text('Keine passenden')
    page.locator('#discovered-search').fill('')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    state['status'] = 'offline'
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#discover')).to_be_disabled()
    state['discovered'][KEY]['observations']['DISCOVER_RESPONSE']['payload'] = {'SNR': None}
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#discovered-list')).to_contain_text('Antwort bei dir: nicht verfügbar')
    expect(page.locator('#discovered-list')).to_contain_text('Anfrage beim Gerät: nicht verfügbar')
    assert not errors, errors
    browser.close()
print('Discovery browser checks passed (desktop and mobile).')
