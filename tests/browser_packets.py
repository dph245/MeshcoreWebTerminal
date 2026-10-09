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
    messages = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    def route(request):
        path = request.request.url.split('http://mesh.test/')[1]
        if path.startswith('api/messages'):
            request.fulfill(json=messages)
        elif path.startswith('api/'):
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
    page.locator('#monitor-nav').click()
    expect(page.locator('#events tr')).to_have_count(5)
    expect(page.locator('#events')).to_contain_text('Absender / Inhalt nicht verfügbar')
    expect(page.locator('#events')).to_contain_text('Dach-Repeater')
    expect(page.locator('#events')).not_to_contain_text('Binäre Anwendungsdaten')
    expect(page.locator('#events tr').filter(has_text='Hallo Mesh').locator('td').nth(3)).to_have_text('cd')
    expect(page.locator('#events tr').filter(has_text='Dach-Repeater').locator('td').nth(3)).to_have_text('Direkt')
    expect(page.locator('#events tr').filter(has_text='Direktnachricht').locator('td').nth(3)).to_have_text('—')
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
    for payload, expected in [
        ({'route_type': 0, 'path_len': 2, 'path_hash_size': 2, 'path': 'aabbCCDD'}, 'ccdd'),
        ({'route_type': 1, 'path_len': 2, 'path_hash_size': 3, 'path': 'aabbccDDEEFF'}, 'ddeeff'),
        ({'route_type': 1, 'path_len': 2, 'path_hash_size': 3, 'path': 'aabbccdd'}, '—'),
        ({'route_type': 1, 'path_len': 1, 'path_hash_size': 4, 'path': 'aabbccdd'}, '—'),
        ({'route_type': 1, 'path_len': 1, 'path_hash_size': 1, 'path': 'zz'}, '—'),
        ({'route_type': 3, 'path_len': 0}, '—'),
        ({'route_type': 1, 'payload_type': 9, 'path_len': 1, 'path_hash_size': 1, 'path': 'ab'}, '—'),
        ({'route_type': 1}, '—'),
    ]:
        event = {'type': 'RX_LOG_DATA', 'time': 1700000020, 'payload': {'payload_type': 5, **payload}}
        page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', event)
        expect(page.locator('#events tr').first.locator('td').nth(3)).to_have_text(expected)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#events tr').filter(has_text='Direktnachricht').get_by_role('button').click()
    expect(page.locator('#packet-fields')).to_contain_text('Routing-Pfad')
    expect(page.locator('#packet-fields')).not_to_contain_text('Empfangspfad')
    page.keyboard.press('Escape')
    for scope, route_type, label in [
        ({'status':'scoped','code':'0x23FA','candidates':['#berlin']}, 0, '#berlin (Code stimmt überein)'),
        ({'status':'scoped','code':'0x479A','candidates':[]}, 0, 'Scope vorhanden · Name unbekannt'),
        ({'status':'scoped','code':'0x479A','candidates':['#eins','#zwei']}, 0, 'Mehrdeutig: #eins, #zwei'),
        ({'status':'unscoped'}, 1, 'Ohne Scope'),
        ({'status':'unknown'}, 0, 'Nicht bestimmbar'),
    ]:
        event = {'type':'RX_LOG_DATA','time':1700000000,'payload':{
            'payload_type':5,'route_type':route_type,'message':'Scope-Test','received_scope':scope}}
        page.evaluate('(event)=>showPacket(event)', event)
        expect(page.locator('#packet-fields')).to_contain_text('Scope des Absenders')
        expect(page.locator('#packet-fields')).to_contain_text(label)
        if scope.get('code'):
            expect(page.locator('#packet-fields')).to_contain_text(scope['code'])
        page.keyboard.press('Escape')
    page.evaluate("showPacket({type:'CHANNEL_MSG_RECV',time:1700000000,payload:{text:'Hallo'}})")
    expect(page.locator('#packet-fields')).to_contain_text('Nicht übermittelt')
    page.keyboard.press('Escape')
    messages[:] = [
        {'id':1,'direction':'in','text':'Mit Scope','timestamp':1700000000,'status':'received',
         'reception':{'routing':'flood','hops':1,'path':['ab'],'scope':{'status':'scoped','code':'0x23FA','candidates':['#berlin']}}},
        {'id':2,'direction':'in','text':'Ohne Scope','timestamp':1700000001,'status':'received',
         'reception':{'routing':'flood','hops':0,'path':[],'scope':{'status':'unscoped'}}},
        {'id':3,'direction':'in','text':'Alter Verlauf','timestamp':1700000002,'status':'received','reception':None},
    ]
    page.evaluate("openChat({kind:'channel',target:'0',name:'Public'})")
    expect(page.locator('.message')).to_have_count(3)
    expect(page.locator('.message-scope')).to_have_count(0)
    expect(page.locator('.message-path')).to_have_text([
        'Pfad: Dach (ab) → Du', 'Pfad: direkt empfangen', 'Pfad: nicht verfügbar'])
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path='/tmp/mesh-chat-scopes.png', full_page=True)
    page.evaluate("openChat({kind:'dm',target:'ab',name:'Dach'})")
    expect(page.locator('.message-scope')).to_have_count(0)
    messages[:] = [
        {'id': 1, 'direction': 'in', 'text': 'Geroutet', 'timestamp': 1700000000, 'status': 'received',
         'reception': {'routing': 'direct', 'hops': None, 'path': None}},
        {'id': 2, 'direction': 'in', 'text': 'Nur Anzahl', 'timestamp': 1700000001, 'status': 'received',
         'reception': {'routing': 'flood', 'hops': 2, 'path': None}},
        {'id': 3, 'direction': 'in', 'text': 'Mehrere Hops', 'timestamp': 1700000002, 'status': 'received',
         'reception': {'routing': 'flood', 'hops': 3, 'path': ['AB', 'cd', 'abcdef']}},
        {'id': 4, 'direction': 'out', 'text': 'Gesendet', 'timestamp': 1700000003, 'status': 'sent'},
    ]
    page.evaluate('loadMessages()')
    expect(page.locator('.message-path')).to_have_text([
        'Pfad: nicht übermittelt (Direct-Routing)',
        'Pfad: 2 Hops · Knotenfolge nicht übermittelt',
        'Pfad: Dach (ab) → cd → abcdef → Du',
        'Pfad: nicht übermittelt'])
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#monitor-nav').click()
    page.evaluate("window.originalNow=Date.now; Date.now=()=>2000000")
    snapshot = {**state, 'last_hops': {
        'aa': {'time': 1990, 'snr': -3}, 'bb': {'time': 1980, 'snr': 8},
        'cc': {'time': 1700, 'snr': 20}, 'dd': {'time': 1995, 'snr': None}}}
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', snapshot)
    page.locator('#monitor-live-nav').focus()
    page.keyboard.press('ArrowRight')
    expect(page.locator('#monitor-hops-nav')).to_be_focused()
    expect(page.locator('#monitor-live')).to_be_hidden()
    expect(page.locator('#last-hops tr td:first-child')).to_have_text(['bb', 'aa', 'dd'])
    page.locator('#monitor-live-nav').click()
    page.locator('#pause').click()
    page.locator('#event-filter').select_option('4')
    page.locator('#monitor-hops-nav').click()
    event = {'type': 'RX_LOG_DATA', 'time': 2000, 'payload': {
        'payload_type': 5, 'route_type': 1, 'path_len': 1, 'path_hash_size': 1, 'path': 'BB', 'snr': -8}}
    page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', event)
    expect(page.locator('#last-hops tr td:first-child')).to_have_text(['aa', 'bb', 'dd'])
    expect(page.locator('#last-hops tr td:nth-child(2)')).to_have_text(['-3', '-8', '—'])
    for width in [1440, 390]:
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=f'/tmp/mesh-last-hops-{width}.png')
    page.evaluate('Date.now=()=>2300000')
    expect(page.locator('#last-hops tr')).to_have_count(0)
    expect(page.locator('#last-hops-empty')).to_be_visible()
    page.evaluate('Date.now=window.originalNow')
    assert not errors, errors
    browser.close()
print('Packet browser checks passed: readable metadata, encrypted/unknown/raw packets, aliases, XSS, live updates, themes and mobile dialog.')
