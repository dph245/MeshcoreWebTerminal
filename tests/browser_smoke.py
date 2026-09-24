"""Run against the local app. Sends only to intercepted, simulated API routes."""
import json
from pathlib import Path
import sys
import time

from playwright.sync_api import sync_playwright, expect

url = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8090'
output = Path('test-results')
output.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(url)
    expect(page.locator('#connection-status')).to_have_text('Verbunden', timeout=20000)
    expect(page.locator('#channels button').first).to_be_visible()
    page.screenshot(path=str(output / 'monitor-desktop.png'), full_page=True)
    page.locator('#channels button').first.click()
    expect(page.locator('#chat')).to_be_visible()
    expect(page.locator('#send')).to_be_disabled()
    page.locator('#message-text').fill('Entwurf – wird nicht gesendet')
    expect(page.locator('#send')).to_be_enabled()
    page.locator('#monitor-nav').click()
    page.locator('#channels button').first.click()
    expect(page.locator('#message-text')).to_have_value('Entwurf – wird nicht gesendet')
    page.locator('#message-text').fill('😀' * 41)
    expect(page.locator('#send')).to_be_disabled()
    page.locator('#message-text').fill('')
    page.locator('#monitor-nav').click()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
    page.screenshot(path=str(output / 'monitor-mobile.png'), full_page=True)
    page.close()

    # Every API request in this context is intercepted, preventing real RF sends.
    page = browser.new_page(viewport={'width': 1200, 'height': 900})
    page.on('pageerror', lambda error: errors.append(str(error)))
    key = 'abcdef123456' + 'ab' * 26
    state = {'host':'192.168.88.14','port':5000,'status':'connected','error':None,
             'channels':[{'index':0,'name':'Public'}],
             'contacts':{key:{'type':1,'adv_name':'Testkontakt','public_key':key}},
             'info':{'name':'Simulierter Companion'},'device':{},'stats':{},'events':[]}
    messages = []
    sent = []
    fail = [False]
    def api_route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        if path == 'events':
            route.fulfill(content_type='text/event-stream', body='event: state\ndata: '+json.dumps(state)+'\n\n')
        elif path.startswith('messages') and request.method == 'GET':
            route.fulfill(json=messages)
        elif path == 'messages' and request.method == 'POST':
            if fail[0]:
                route.fulfill(status=502,json={'detail':'Simulierter Funkfehler'})
                return
            data=request.post_data_json
            sent.append(data)
            messages.append({'id':len(sent),'direction':'out','text':data['text'],'timestamp':time.time(),'status':'sent','target':data['target']})
            route.fulfill(json={'id':len(sent),'status':'sent'})
        else:
            route.fulfill(json=state)
    page.route('**/api/**', api_route)
    # Keep the synthetic stream from triggering reconnect/error between actions.
    page.add_init_script("""window.EventSource = class {
      constructor() { this.listeners = {}; setTimeout(async () => {
        const state = await (await fetch('/api/state')).json();
        this.listeners.state?.({data:JSON.stringify(state)});
      }, 100); }
      addEventListener(name, cb) { this.listeners[name] = cb; }
    };""")
    page.goto(url)
    page.locator('#channels button').click()
    page.locator('#message-text').fill('<img src=x onerror=alert(1)> Moin')
    page.locator('#send').click()
    expect(page.locator('.bubble')).to_have_text('<img src=x onerror=alert(1)> Moin')
    assert page.locator('.bubble img').count() == 0
    assert sent[-1]['kind'] == 'channel'
    expect(page.locator('#message-text')).to_have_value('')
    messages.clear()
    page.locator('#contacts button').click()
    page.locator('#message-text').fill('Hallo Testkontakt')
    page.locator('#send').click()
    expect(page.locator('.bubble')).to_have_text('Hallo Testkontakt')
    assert sent[-1]['kind'] == 'dm'
    assert sent[-1]['target'] == key
    fail[0] = True
    page.locator('#message-text').fill('Entwurf bleibt bei Fehler')
    page.locator('#send').click()
    expect(page.locator('#error')).to_have_text('Simulierter Funkfehler')
    expect(page.locator('#message-text')).to_have_value('Entwurf bleibt bei Fehler')
    assert len(sent) == 2
    assert not errors, errors
    browser.close()
    print('Browser OK: live connection, desktop/mobile, drafts, UTF-8 limit, mocked channel/DM sends, XSS and error recovery.')
