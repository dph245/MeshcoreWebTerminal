"""Manual TRACE editor and history, intercepted HTTP only (no radio traffic)."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
KEY = 'ab' * 32
state = dict(status='connected', host='test', port=5000, channels=[], contacts={
    KEY: {'type': 2, 'adv_name': '<Relay>'}}, events=[], info={}, device={}, stats={}, traces=[])
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
            state['traces'].insert(0, dict(id=f'{len(requests):032x}', timestamp=1791500000,
                                         status='partial', path=['ab', 'cd', 'ab'], names=['<Relay>', None, '<Relay>'],
                                         snrs=[-2.25, 0, None, 1.25]))
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
    page.locator('#mesh-nav').click()
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#manual-trace')).to_be_hidden()
    expect(page.locator('#mesh-discovery')).to_be_visible()
    page.locator('#manual-trace-nav').click()
    expect(page.locator('#mesh-discovery')).to_be_hidden()
    expect(page.locator('#trace-send')).to_be_disabled()
    page.locator('#trace-hops input').fill(KEY)
    # Incoming events must not replace options or disturb an active draft.
    page.locator('#trace-hops input').focus()
    page.evaluate("""() => {
      window.choiceMutations=[];
      window.choiceObserver=new MutationObserver(records=>choiceMutations.push(...records));
      for(const id of ['trace-contacts','repeater-target'])
        choiceObserver.observe(document.getElementById(id), {childList:true,subtree:true,attributes:true,characterData:true});
    }""")
    state['contacts'][KEY]['last_advert'] = 1234
    page.evaluate("""data => {
      testStream.listeners.message({data:JSON.stringify({conversations:[]})});
      testStream.listeners.state({data:JSON.stringify(data)});
      testStream.listeners.radio({data:JSON.stringify({type:'RX_LOG_DATA',time:1234,payload:{}})});
    }""", state)
    expect(page.locator('#trace-hops input')).to_be_focused()
    expect(page.locator('#trace-hops input')).to_have_value(KEY)
    assert page.evaluate('choiceMutations.length') == 0
    page.evaluate('choiceObserver.disconnect()')
    # Actual contact changes must still refresh both lists.
    state['contacts'][KEY]['adv_name'] = 'Renamed relay'
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#trace-contacts option')).to_have_attribute('label', 'Renamed relay')
    expect(page.locator('#repeater-target option')).to_have_text('Renamed relay')
    page.locator('#trace-add').click()
    page.locator('#trace-hops input').nth(1).fill('cd')
    page.locator('#trace-return').check()
    expect(page.locator('#trace-preview')).to_have_text('Companion → ab → cd → ab → Companion')
    page.get_by_role('button', name='Hop 2 nach oben', exact=True).click()
    expect(page.locator('#trace-preview')).to_have_text('Companion → cd → ab → cd → Companion')
    page.get_by_role('button', name='Hop 1 nach unten', exact=True).click()
    page.locator('#trace-add').click()
    page.locator('#trace-hops input').nth(2).fill('ab')
    page.get_by_role('button', name='Hop 3 entfernen', exact=True).click()
    page.locator('#manual-trace-nav').press('Home')
    expect(page.locator('#mesh-discovery')).to_be_visible()
    page.locator('#mesh-discovery-nav').press('ArrowRight')
    expect(page.locator('#manual-trace-nav')).to_be_focused()
    expect(page.locator('#manual-trace-nav')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#trace-preview')).to_have_text('Companion → ab → cd → ab → Companion')
    assert requests == []
    page.locator('#trace-send').click()
    expect(page.locator('#trace-feedback')).to_have_text('TRACE einmal gesendet.')
    assert requests == [('api/trace', {'path': [KEY, 'cd'], 'hash_size': 1, 'return_path': True})]
    page.locator('#trace-results summary').click()
    expect(page.locator('#trace-results tbody tr')).to_have_count(4)
    expect(page.locator('#trace-results')).to_contain_text('<Relay> (ab)')
    expect(page.locator('#trace-results')).to_contain_text('-2,25')
    expect(page.locator('#trace-results')).to_contain_text('Fehlt')
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#trace-results details')).to_have_attribute('open', '')
    page.locator('#trace-send').click()
    expect(page.locator('#trace-results details')).to_have_count(2)
    assert len(requests) == 2
    page.locator('#trace-hops input').nth(1).fill('zz')
    expect(page.locator('#trace-send')).to_be_disabled()
    page.locator('#trace-hops input').nth(1).fill('cd')
    page.locator('#trace-size').select_option('2')
    expect(page.locator('#trace-send')).to_be_disabled()
    page.locator('#trace-size').select_option('1')
    state['status'] = 'offline'
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#trace-send')).to_be_disabled()
    for width in [1280, 768, 390]:
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
    assert len(requests) == 2
    assert not errors, errors
    browser.close()
print('Manual TRACE browser checks passed')
