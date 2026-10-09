"""Contact management UI checks with simulated API; no radio traffic."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
KEY = 'ab' * 32
state = {'status': 'connected', 'host': 'test', 'port': 5000, 'channels': [],
         'contacts': {}, 'events': [], 'info': {}, 'device': {}, 'stats': {}}
requests = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def route(r):
        path = r.request.url.split('http://mesh.test/')[1]
        if r.request.method == 'POST':
            body = r.request.post_data_json
            requests.append((path, body))
            if path == 'api/contacts':
                key = body['target'].lower()
                if key in state['contacts']:
                    r.fulfill(status=409, json={'detail': 'Bereits gespeichert.'})
                    return
                state['contacts'][key] = {'adv_name': body['name'], 'type': body['type']}
            elif path == 'api/contacts/remove':
                del state['contacts'][body['target']]
            else:
                raise AssertionError(path)
            r.fulfill(json=state)
        elif path.startswith('api/messages'):
            r.fulfill(json=[])
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

    def refresh():
        page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)

    refresh()
    page.locator('#device-nav').click()
    page.locator('#contact-manager-nav').click()
    expect(page.locator('#contact-manager')).to_be_visible()
    page.locator('#new-contact-name').fill('<Alice>')
    page.locator('#new-contact-key').fill(KEY.upper())
    refresh()  # Live events must preserve the form draft.
    expect(page.locator('#new-contact-name')).to_have_value('<Alice>')
    page.locator('#add-contact').click()
    expect(page.locator('#contact-feedback')).to_contain_text('gespeichert und bestätigt')
    assert requests[-1] == ('api/contacts', {'target': KEY.upper(), 'name': '<Alice>', 'type': 1})
    expect(page.locator('#contacts')).to_contain_text('<Alice>')
    expect(page.locator('#new-contact-name')).to_have_value('')
    page.locator('#managed-contact-search').fill('absent')
    expect(page.locator('#managed-contacts')).to_contain_text('Keine passenden')
    page.locator('#managed-contact-search').fill('')
    page.locator('#new-contact-name').fill('Duplicate')
    page.locator('#new-contact-key').fill(KEY)
    page.locator('#add-contact').click()
    expect(page.locator('#contact-feedback')).to_contain_text('Bereits gespeichert')
    expect(page.locator('#new-contact-name')).to_have_value('Duplicate')
    page.locator('#terminal-nav').click()
    page.locator('#contacts button').click()
    expect(page.locator('#chat')).to_be_visible()
    state['contacts'] = {}
    refresh()  # Deletion by another client closes the now invalid chat.
    expect(page.locator('#chat')).to_be_hidden()
    expect(page.locator('#terminal')).to_be_visible()
    state['contacts'] = {KEY: {'adv_name': '<Alice>', 'type': 1}}
    refresh()
    page.locator('#device-nav').click()
    page.locator('#managed-contacts button').click()
    expect(page.locator('#contact-feedback')).to_contain_text('gelöscht und bestätigt')
    expect(page.locator('#managed-contact-count')).to_have_text('0')
    assert requests[-1] == ('api/contacts/remove', {'target': KEY})
    # Each supported type appears in the manager and can be removed.
    for kind in (2, 3, 4):
        page.locator('#new-contact-name').fill('Node')
        page.locator('#new-contact-key').fill(KEY)
        page.locator('#new-contact-type').select_option(str(kind))
        page.locator('#add-contact').click()
        expect(page.locator('#managed-contact-count')).to_have_text('1')
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#managed-contacts button').click()
        expect(page.locator('#managed-contact-count')).to_have_text('0')
    state['status'] = 'offline'
    refresh()
    expect(page.locator('#add-contact')).to_be_disabled()
    expect(page.locator('#new-contact-key')).to_be_disabled()
    assert not errors, errors
    browser.close()
print('Contact management browser checks passed (desktop and mobile).')
