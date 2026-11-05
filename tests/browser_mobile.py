"""Mobile terminal layout with touch emulation and intercepted APIs; no radio."""
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
CONTACT, ROOM = 'ab' * 32, 'cd' * 32
state = dict(status='connected', host='test', port=5000, events=[], info={}, device={'fw ver':13}, stats={},
             channels=[dict(index=i, name=f'#Channel {i}') for i in range(32)],
             contacts={CONTACT:dict(type=1, adv_name='Ein Kontakt mit einem sehr langen Namen'),
                       ROOM:dict(type=3, adv_name='Roomserver mit langem Namen')},
             rooms={}, scopes={'default':'','supported':True,'channels':{}})

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport=dict(width=390,height=844), is_mobile=True, has_touch=True)
    errors, writes = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))

    def route(r):
        path = urlparse(r.request.url).path
        if r.request.method == 'POST':
            writes.append(path)
            raise AssertionError(f'Unexpected write: {path}')
        if path == '/api/messages':
            r.fulfill(json=[dict(id=i,direction='in',text=f'Radio: Nachricht {i}',
                                target=CONTACT,sender_key=CONTACT[:8],timestamp=100+i,status='received') for i in range(1,31)])
        elif path.startswith('/api/'):
            r.fulfill(json=state)
        else:
            r.fulfill(path=STATIC/(path.lstrip('/') or 'index.html'))

    page.route('**/*', route)
    page.add_init_script('''window.EventSource=class {
      constructor(){this.listeners={};window.testStream=this;}
      addEventListener(name,callback){this.listeners[name]=callback;}
    };''')
    page.goto('http://mesh.test/')
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})',state)
    expect(page.locator('#conversation-list')).to_be_visible()
    expect(page.locator('.terminal-content')).to_be_hidden()
    expect(page.locator('#conversation-list select')).to_have_count(4)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

    def fits():
        page.wait_for_function('''()=>document.documentElement.scrollHeight<=innerHeight+1
          && document.documentElement.scrollWidth<=innerWidth''')
        expect(page.locator('#composer')).to_be_in_viewport(ratio=1)
        assert page.locator('#messages').evaluate('(el)=>el.clientHeight>0')

    for width,height in [(390,844),(320,568),(667,375),(390,420)]:
        page.set_viewport_size(dict(width=width,height=height))
        page.locator('#contacts').select_option(page.locator('#contacts option').nth(1).get_attribute('value'))
        expect(page.locator('#conversation-list')).to_be_hidden()
        expect(page.locator('#chat-route-edit')).to_be_visible()
        page.locator('#message-text').fill('Mobiler Entwurf')
        fits()
        page.locator('#emoji-toggle').click()
        fits()
        page.screenshot(path=f'/tmp/mesh-mobile-emojis-{width}-{height}.png')
        page.get_by_role('button',name='Pizza 🍕',exact=True).click()
        expect(page.locator('#message-text')).to_have_value('Mobiler Entwurf🍕')
        fits()
        page.screenshot(path=f'/tmp/mesh-mobile-chat-{width}-{height}.png')
        page.locator('#conversation-back').click()
        expect(page.locator('.terminal-content')).to_be_hidden()
        page.screenshot(path=f'/tmp/mesh-mobile-conversations-{width}-{height}.png')
        page.locator('#conversation-return').click()
        expect(page.locator('#message-text')).to_have_value('Mobiler Entwurf🍕')
        fits()
        page.locator('#conversation-back').click()
        page.locator('#channels').select_option(page.locator('#channels option').nth(1).get_attribute('value'))
        expect(page.locator('#channel-scope-form')).to_be_hidden()
        page.locator('#chat-options-toggle').click()
        expect(page.locator('#channel-scope-form')).to_be_visible()
        fits()
        page.locator('#chat-options-toggle').click()
        page.locator('#conversation-back').click()
        page.locator('#rooms').select_option(page.locator('#rooms option').nth(1).get_attribute('value'))
        expect(page.locator('#room-login-form')).to_be_hidden()
        page.locator('#chat-options-toggle').click()
        expect(page.locator('#room-login-form')).to_be_visible()
        fits()
        page.locator('#chat-options-toggle').click()
        page.locator('#conversation-back').click()
    assert not writes
    assert not errors, errors
    browser.close()
print('Mobile terminal checks passed (touch, small screens, landscape, reduced viewport; no writes).')
