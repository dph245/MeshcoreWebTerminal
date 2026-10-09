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
    page.locator('#device-nav').click()
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#cli-command-search')).to_be_hidden()
    page.locator('#cli-reference > summary').click()
    expect(page.locator('#cli-command-list')).to_contain_text('neighbors')
    expect(page.locator('#cli-command-list')).to_contain_text('get allow.read.only')
    state['contacts'][KEY] = {'type': 2, 'adv_name': 'Relay', 'public_key': KEY}
    state['repeaters'] = {}
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#cli-command-list')).not_to_contain_text('get allow.read.only')
    page.locator('#cli-command-search').fill('SET FREQ')
    expect(page.locator('#cli-command-list tr')).to_have_count(1)
    expect(page.locator('#cli-command-list')).to_contain_text('set freq <frequency>')
    expect(page.locator('#cli-command-list td:first-child > div').filter(has_text='set freq')).to_contain_text('Nur Serial')
    expect(page.locator('#cli-command-list td:first-child > div').filter(has_text='get freq')).not_to_contain_text('Nur Serial')
    page.locator('#cli-command-search').fill('kein-solches-kommando')
    expect(page.locator('#cli-command-empty')).to_be_visible()
    page.locator('#cli-command-search').fill('')
    room_key = 'cd' * 32
    state['contacts'][room_key] = {'type': 3, 'adv_name': 'Room', 'public_key': room_key}
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    page.locator('#repeater-target').select_option(room_key)
    expect(page.locator('#cli-command-list')).to_contain_text('get allow.read.only')
    expect(page.locator('#cli-command-list')).not_to_contain_text('discover.neighbors')
    expect(page.locator('#cli-command-list')).not_to_contain_text('powersaving')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path='/tmp/mesh-cli-reference-mobile.png', full_page=True)
    assert requests == [], requests  # Browsing the reference sends no commands.
    page.locator('#repeater-target').select_option(KEY)
    page.locator('#cli-reference > summary').click()
    expect(page.locator('#repeater-send')).to_be_disabled()
    page.locator('#repeater-password').fill('secret')
    page.locator('#repeater-login').click()
    expect(page.locator('#repeater-password')).to_have_value('')
    assert requests[-1] == ('api/repeaters', {'target': KEY, 'action': 'login', 'value': 'secret'})
    expect(page.locator('#repeater-login')).to_be_enabled()  # Wait for the login POST to finish before simulating its confirmation.
    state['repeaters'][KEY] = {'status': 'logged_in', 'replies': []}
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    page.locator('#repeater-command').fill('get name')
    page.locator('#repeater-send').click()
    expect(page.locator('#repeater-feedback')).to_contain_text('Ausführung noch nicht bestätigt')
    assert requests[-1] == ('api/repeaters', {'target': KEY, 'action': 'command', 'value': 'get name'})
    state['repeaters'][KEY]['replies'] = [{'time': 123, 'text': '<img src=x onerror=alert(1)>'}]
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#repeater-replies')).to_contain_text('<img src=x onerror=alert(1)>')
    expect(page.locator('#repeater-replies img')).to_have_count(0)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    state['status'] = 'offline'
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#repeater-send')).to_be_disabled()
    expect(page.locator('#repeater-login')).to_be_disabled()
    assert not errors, errors
    browser.close()
print('Repeater CLI browser checks passed (simulated API).')
