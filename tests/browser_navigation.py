"""Unread markers and conversation sorting, with simulated API and no radio traffic."""
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
ALICE, ZOE = 'ab' * 32, 'cd' * 32
state = dict(status='connected', host='test', port=5000, events=[], info={}, device={}, stats={},
             channels=[dict(index=0, name='#Zulu'), dict(index=1, name='#Alpha')],
             contacts={ZOE: dict(type=1, adv_name='Zoe'), ALICE: dict(type=1, adv_name='Alice')},
             conversations=[])
messages = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport=dict(width=1280, height=900))
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))

    def route(r):
        url = urlparse(r.request.url)
        if url.path == '/api/messages':
            query = parse_qs(url.query)
            r.fulfill(json=[m for m in messages if m['kind'] == query['kind'][0]
                            and m['target'] == query['target'][0][:12]])
        elif url.path.startswith('/api/'):
            r.fulfill(json=state)
        else:
            r.fulfill(path=STATIC / (url.path.lstrip('/') or 'index.html'))

    page.route('**/*', route)
    page.add_init_script('''window.EventSource=class {
      constructor(){this.listeners={};window.testStream=this;}
      addEventListener(name,callback){this.listeners[name]=callback;}
    };''')

    def refresh():
        page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)

    def message(kind, target, direction='in'):
        mid = len(messages) + 1
        messages.append(dict(id=mid, kind=kind, target=target, direction=direction,
                             text=f'Message {mid}', timestamp=100+mid, status='received'))
        existing = next((c for c in state['conversations'] if c['kind'] == kind and c['target'] == target), None)
        if existing is None:
            existing = dict(kind=kind, target=target, incoming_id=0)
            state['conversations'].append(existing)
        existing.update(latest_id=mid, last_activity=100+mid)
        if direction == 'in':
            existing['incoming_id'] = mid
        page.evaluate('(data)=>testStream.listeners.message({data:JSON.stringify(data)})',
                      dict(conversations=state['conversations']))

    page.goto('http://mesh.test/')
    refresh()
    expect(page.locator('#channels .nav-name')).to_have_text(['Alpha', 'Zulu'])
    expect(page.locator('#contacts .nav-name')).to_have_text(['Alice', 'Zoe'])
    message('channel', '0')
    message('dm', ZOE[:12])
    message('dm', ALICE[:12], 'out')
    expect(page.locator('#channels .unread-dot')).to_have_count(1)
    expect(page.locator('#contacts .unread-dot')).to_have_count(1)
    page.locator('#conversation-sort').select_option('recent')
    expect(page.locator('#channels .nav-name')).to_have_text(['Zulu', 'Alpha'])
    expect(page.locator('#contacts .nav-name')).to_have_text(['Alice', 'Zoe'])
    page.get_by_role('button', name='Zoe').click()
    expect(page.locator('#messages')).to_contain_text('Message 2')
    expect(page.locator('#contacts .unread-dot')).to_have_count(0)
    expect(page.locator('#contacts .nav-name')).to_have_text(['Zoe', 'Alice'])
    message('dm', ZOE[:12])
    expect(page.locator('#messages')).to_contain_text('Message 4')
    expect(page.locator('#contacts .unread-dot')).to_have_count(0)
    page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'})")
    message('dm', ZOE[:12])
    expect(page.locator('#messages')).to_contain_text('Message 5')
    expect(page.locator('#contacts .unread-dot')).to_have_count(1)
    page.evaluate("delete document.visibilityState; document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator('#contacts .unread-dot')).to_have_count(0)
    page.reload()
    refresh()
    expect(page.locator('#conversation-sort')).to_have_value('recent')
    expect(page.locator('#channels .unread-dot')).to_have_count(1)
    expect(page.locator('#contacts .unread-dot')).to_have_count(0)
    message('dm', ZOE[:12])
    expect(page.locator('#contacts .unread-dot')).to_have_count(1)
    page.locator('#contact-search').fill('Zoe')
    expect(page.locator('#contacts .nav-name')).to_have_text(['Zoe'])
    page.locator('#contact-search').fill('')
    page.locator('#conversation-sort').select_option('abc')
    expect(page.locator('#contacts .nav-name')).to_have_text(['Alice', 'Zoe'])
    page.set_viewport_size(dict(width=390, height=844))
    expect(page.locator('#conversation-sort')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    browser.close()
print('Unread and sorting checks passed (desktop/mobile, simulated API).')
