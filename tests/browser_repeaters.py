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
    state['contacts'][KEY] = {'type': 2, 'adv_name': 'Relay', 'public_key': KEY}
    state['repeaters'] = {}
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#repeater-send')).to_be_disabled()
    page.locator('#repeater-password').fill('secret')
    page.locator('#repeater-login').click()
    expect(page.locator('#repeater-password')).to_have_value('')
    assert requests[-1] == ('api/repeaters', {'target': KEY, 'action': 'login', 'value': 'secret'})
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
