"""Offline browser checks: all HTTP requests are intercepted; no radio traffic."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
fixtures = [
    {'payload_type': 5, 'route_type': 1, 'path_len': 2, 'path_hash_size': 1, 'path': 'abcd',
     'pkt_payload': 'aabbcc', 'payload_ver': 0, 'chan_name': '#test', 'message': '<img src=x onerror=alert(1)> Hallo Mesh', 'rssi': -103, 'snr': 4.5},
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
    expect(page.locator('#events')).to_contain_text('Inhalt verschlüsselt')
    expect(page.locator('#events')).to_contain_text('Dach-Repeater')
    expect(page.locator('#events')).not_to_contain_text('Binäre Anwendungsdaten')
    row = page.locator('#events tr').filter(has_text='Hallo Mesh')
    expect(row.locator('td').nth(3)).to_have_text('cd')
    expect(row.locator('.packet-message')).to_have_text(fixtures[0]['message'])
    expect(row.locator('.packet-fields')).to_contain_text('Dach (ab) → cd')
    expect(row.locator('.packet-fields')).to_contain_text('-103 dBm')
    expect(row.locator('.packet-fields')).to_contain_text('4.5 dB')
    expect(row.locator('td').first.locator('button')).to_have_count(0)
    assert page.locator('#events img').count() == 0
    expect(row.locator('.packet-raw')).not_to_have_attribute('open', '')
    row.locator('.packet-raw summary').click()
    # Same payload through another hop remains one group; latest signal wins.
    duplicate = {'type':'RX_LOG_DATA','time':1700000030,'payload':{
        **fixtures[0], 'pkt_payload':'AABBCC', 'path':'abef', 'rssi':-90, 'snr':8}}
    page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', duplicate)
    expect(page.locator('#events tr')).to_have_count(5)
    expect(row.locator('.packet-raw')).to_have_attribute('open', '')
    expect(row.locator('td').nth(1)).to_have_text('Channel-Nachricht · 2×')
    assert row.locator('td').nth(1).evaluate('el => el.scrollWidth<=el.clientWidth')
    expect(row.locator('td').nth(3)).to_have_text('ef')
    expect(row.locator('td').nth(4)).to_have_text('-90')
    expect(row.locator('.packet-receptions li')).to_have_count(2)
    expect(row.locator('.packet-receptions')).to_contain_text('Dach (ab) → cd')
    expect(row.locator('.packet-receptions')).to_contain_text('Dach (ab) → ef')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    row.locator('.packet-raw summary').click()
    page.screenshot(path='/tmp/mesh-live-packets-grouped-desktop.png')
    # TRACE variants and unrelated login attempts must remain separate.
    assert page.evaluate("""()=>{
        const trace={type:'RX_LOG_DATA',payload:{payload_type:9,payload_ver:0,pkt_payload:'abcd',path_len:1,path_hash_size:1,path:'ab'}};
        return groupPackets([trace,{...trace,payload:{...trace.payload,path:'cd'}}]).length===2
            && groupPackets([trace,{...trace,payload:{...trace.payload,path:'AB'}}]).length===1
            && groupPackets([{type:'AUTH_LOGIN'},{type:'AUTH_LOGIN'}]).length===2;
    }""")
    # Exact bytes, type and version distinguish packets; decoded text alone does not.
    for changes in [{'pkt_payload':'aabbcd'}, {'payload_type':2}, {'payload_ver':1}, {'pkt_payload':''}, {'pkt_payload':'zz'}]:
        event = {**duplicate, 'payload':{**duplicate['payload'],**changes}}
        page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', event)
    expect(page.locator('#events tr')).to_have_count(10)
    # Pause is a fixed snapshot, including filtering and grouped counts.
    page.locator('#pause').click()
    page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', duplicate)
    page.locator('#event-filter').select_option('5')
    expect(page.locator('#events td:nth-child(2)').filter(has_text='3×')).to_have_count(0)
    page.locator('#pause').click()
    expect(page.locator('#events td:nth-child(2)').filter(has_text='3×')).to_have_count(1)
    page.locator('#event-filter').select_option('all')
    for theme in ['dark', 'light']:
        page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=f'/tmp/mesh-live-packets-{theme}.png')
    # A state refresh groups the same receptions just like live events.
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#events tr')).to_have_count(5)
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
        event = {'type': 'RX_LOG_DATA', 'time': 1700000040, 'payload': {'payload_type': 5, **payload}}
        page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', event)
        expect(page.locator('#events tr').first.locator('td').nth(3)).to_have_text(expected)
    direct = page.locator('#events tr').filter(has_text='Direktnachricht').locator('.packet-fields')
    expect(direct).to_contain_text('Routing-Pfad')
    expect(direct).not_to_contain_text('Empfangspfad')
    for scope, route_type, label in [
        ({'status':'scoped','code':'0x23FA','candidates':['#berlin']}, 0, '#berlin (Code stimmt überein)'),
        ({'status':'scoped','code':'0x479A','candidates':[]}, 0, 'Scope vorhanden · Name unbekannt'),
        ({'status':'scoped','code':'0x479A','candidates':['#eins','#zwei']}, 0, 'Mehrdeutig: #eins, #zwei'),
        ({'status':'unscoped'}, 1, 'Ohne Scope'),
        ({'status':'unknown'}, 0, 'Nicht bestimmbar'),
    ]:
        event = {'type':'RX_LOG_DATA','time':1700000050,'payload':{
            'payload_type':5,'route_type':route_type,'message':'Scope-Test','received_scope':scope}}
        page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', event)
        fields=page.locator('#events tr').first.locator('.packet-fields')
        expect(fields).to_contain_text('Scope des Absenders')
        expect(fields).to_contain_text(label)
        if scope.get('code'):
            expect(fields).to_contain_text(scope['code'])
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
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
    state['contacts'].update({
        'b978'+'0'*60: {'adv_name': 'DE-NI-WF-Ost', 'type': 2},
        'dd'+'0'*62: {'adv_name': 'Mehrdeutig Ost', 'type': 2},
        'dd'+'1'*62: {'adv_name': 'Mehrdeutig West', 'type': 2},
    })
    snapshot = {**state, 'last_hops': {
        'b978': {'time': 1990, 'snr': -3, 'rssi': -99}, 'bb': {'time': 1980, 'snr': 8, 'rssi': -87},
        'cc': {'time': 1700, 'snr': 20}, 'dd': {'time': 1995, 'snr': None}}}
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', snapshot)
    page.locator('#monitor-live-nav').focus()
    page.keyboard.press('ArrowRight')
    expect(page.locator('#monitor-hops-nav')).to_be_focused()
    expect(page.locator('#monitor-live')).to_be_hidden()
    expect(page.locator('#last-hops tr td:first-child')).to_have_text(['bb', 'DE-NI-WF-Ost (b978)', 'dd'])
    expect(page.locator('#last-hops tr td:nth-child(3)')).to_have_text(['-87', '-99', '—'])
    page.locator('#monitor-live-nav').click()
    page.locator('#pause').click()
    page.locator('#event-filter').select_option('4')
    page.locator('#monitor-hops-nav').click()
    event = {'type': 'RX_LOG_DATA', 'time': 2000, 'payload': {
        'payload_type': 5, 'route_type': 1, 'path_len': 1, 'path_hash_size': 1, 'path': 'BB', 'snr': -8, 'RSSI': -112}}
    page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', event)
    expect(page.locator('#last-hops tr td:first-child')).to_have_text(['DE-NI-WF-Ost (b978)', 'bb', 'dd'])
    expect(page.locator('#last-hops tr td:nth-child(2)')).to_have_text(['-3', '-8', '—'])
    expect(page.locator('#last-hops tr td:nth-child(3)')).to_have_text(['-99', '-112', '—'])
    for width in [1440, 390]:
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=f'/tmp/mesh-last-hops-{width}.png')
    page.evaluate('Date.now=()=>2300000')
    expect(page.locator('#last-hops tr')).to_have_count(0)
    expect(page.locator('#last-hops-empty')).to_be_visible()
    page.evaluate('Date.now=window.originalNow')
    page.locator('#monitor-live-nav').click()
    page.locator('#pause').click()
    login = {'time': 1700000020, 'type': 'AUTH_LOGIN', 'payload': {
        'target': 'ab'+'0'*62, 'attempt': 2, 'mode': 'AUTO', 'route': 'flood',
        'result': 'success', 'timeout': 60}}
    page.evaluate('(event)=>testStream.listeners.radio({data:JSON.stringify(event)})', login)
    page.locator('#event-filter').select_option('auth')
    expect(page.locator('#events tr')).to_have_count(1)
    expect(page.locator('#events')).to_contain_text('Dach · Versuch 2/3 · AUTO / FLOOD · Erfolgreich')
    expect(page.locator('#events .packet-fields')).to_contain_text('60 s')
    expect(page.locator('#events .packet-message')).to_have_count(0)
    page.locator('#event-filter').select_option('5')
    expect(page.locator('#events')).not_to_contain_text('Versuch 2/3')
    assert not errors, errors
    browser.close()
print('Packet browser checks passed: readable metadata, encrypted/unknown/raw packets, aliases, XSS, live updates, grouping, pause, themes and inline mobile details.')
