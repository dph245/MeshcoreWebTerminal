"""Offline browser checks with static files and intercepted, simulated API routes."""
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
    errors = []
    # Every API request in this context is intercepted, preventing real RF sends.
    page = browser.new_page(viewport={'width': 1200, 'height': 900})
    page.on('pageerror', lambda error: errors.append(str(error)))
    key = 'abcdef123456' + 'ab' * 26
    state = {'host':'192.168.88.14','port':5000,'status':'connected','error':None,
             'channels':[{'index':0,'name':'Public'}],
             'contacts':{key:{'type':1,'adv_name':'Testkontakt','public_key':key}},
             'info':{'name':'Simulierter Companion'},'device':{'fw ver':13},'stats':{},'events':[],
             'scopes':{'default':'#test','supported':True,'channels':{}}}
    messages = []
    sent = []
    fail = [False]
    def api_route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        if path == 'events':
            route.fulfill(content_type='text/event-stream', body='event: state\ndata: '+json.dumps(state)+'\n\n')
        elif path == 'scopes':
            data=request.post_data_json
            if data['channel'] is None:
                state['scopes']['default']=data['scope']
            else:
                state['scopes']['channels'][data['channel']]=data['scope']
            route.fulfill(json=state)
        elif path == 'channels':
            data=request.post_data_json
            state['channels'].append({'index':1,'name':'#'+data['name'].lstrip('#')})
            route.fulfill(json=state)
        elif path == 'channels/remove':
            data=request.post_data_json
            state['channels']=[c for c in state['channels'] if c['index']!=data['index']]
            route.fulfill(json=state)
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
    page.route('**/*', lambda r: r.fulfill(path=Path(__file__).resolve().parents[1] / 'static' / (r.request.url.split(url)[1].lstrip('/') or 'index.html')))
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
    page.locator('#device-nav').click()
    page.locator('#channel-manager-nav').click()
    page.locator('#new-channel-name').fill('browsertest')
    page.locator('#add-channel').click()
    expect(page.locator('#channel-feedback')).to_have_text('Channel im Companion gespeichert.')
    expect(page.locator('#channels button')).to_have_count(2)
    page.get_by_role('button', name='#browsertest vom Companion entfernen').click()
    expect(page.locator('#channel-feedback')).to_contain_text('Channel vom Companion entfernt.')
    expect(page.locator('#channels button')).to_have_count(1)
    page.locator('#companion-settings-nav').click()
    expect(page.locator('#default-scope-input')).to_have_value('#test')
    page.locator('#default-scope-input').fill('#region')
    page.locator('#default-scope-save').click()
    expect(page.locator('#default-scope-feedback')).to_have_text('Gespeichert.')
    assert state['scopes']['default'] == '#region'
    page.locator('#default-scope-clear').click()
    expect(page.locator('#default-scope-current')).to_have_text('Ohne Scope')
    page.locator('#terminal-nav').click()
    page.locator('#channels button').click()
    expect(page.locator('.chat-heading #channel-scope-form')).to_be_visible()
    expect(page.locator('#device #channel-scope-form')).to_have_count(0)
    page.locator('#channel-scope-mode').select_option('region')
    page.locator('#channel-scope-input').fill('#local')
    page.locator('#channel-scope-save').click()
    expect(page.locator('#channel-scope-current')).to_have_text('Aktiv: #local')
    assert state['scopes']['channels']['0'] == '#local'
    assert state['scopes']['default'] == ''
    state['channels'].append({'index': 2, 'name': '#anderer'})
    page.evaluate('(data)=>stream.listeners.state({data:JSON.stringify(data)})', state)
    page.locator('#channels button').filter(has_text='anderer').click()
    expect(page.locator('#channel-scope-mode')).to_have_value('default')
    expect(page.locator('#channel-scope-input')).to_have_value('')
    page.locator('#channels button').filter(has_text='Public').click()
    expect(page.locator('#channel-scope-input')).to_have_value('#local')
    page.locator('#channel-scope-mode').select_option('unscoped')
    page.locator('#channel-scope-save').click()
    expect(page.locator('#channel-scope-current')).to_have_text('Aktiv: Ohne Scope')
    page.locator('#terminal-nav').click()
    page.locator('#channels button').filter(has_text='Public').click()
    page.locator('#message-text').fill('<img src=x onerror=alert(1)> Moin')
    page.locator('#send').click()
    expect(page.locator('.bubble')).to_have_text('<img src=x onerror=alert(1)> Moin')
    assert page.locator('.bubble img').count() == 0
    assert sent[-1]['kind'] == 'channel'
    expect(page.locator('#message-text')).to_have_value('')
    expect(page.locator('.message-meta')).to_contain_text('An Companion übergeben')
    for count in range(1,5):
        messages[0]['repeater_count']=count
        page.evaluate("stream.listeners.message({data:'{}'})")
        expect(page.locator('.message-meta')).to_contain_text(f'von {count} '+('Repeater' if count==1 else 'Repeatern')+' empfangen')
    messages.clear()
    page.locator('#contacts button').click()
    expect(page.locator('#channel-scope-form')).to_be_hidden()
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
    messages[:] = [
        {'id':10,'direction':'in','text':'Mit Empfangspfad','timestamp':time.time(),'status':'received','target':key,
         'reception':{'routing':'flood','hops':2,'path':['ab0012','cd0034']}},
        {'id':11,'direction':'in','text':'Geroutete DM','timestamp':time.time(),'status':'received','target':key,
         'reception':{'routing':'direct','hops':None,'path':None}},
        {'id':12,'direction':'in','text':'Direkter Empfang','timestamp':time.time(),'status':'received','target':key,
         'reception':{'routing':'flood','hops':0,'path':[]}},
        {'id':13,'direction':'in','text':'Alter Verlauf','timestamp':time.time(),'status':'received','target':key,'reception':None},
    ]
    page.locator('#contacts button').click()
    expect(page.locator('#channel-scope-form')).to_be_hidden()
    expect(page.locator('.message-path')).to_have_count(0)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Chat path mobile overflow'
    page.screenshot(path=str(output / 'chat-path-mobile.png'), full_page=True)
    assert not errors, errors
    browser.close()
    print('Browser OK: simulated channel management, scopes, channel/DM sends, UTF-8 limit, XSS, errors and mobile layout.')
