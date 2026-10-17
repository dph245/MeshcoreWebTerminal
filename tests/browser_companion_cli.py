"""Companion CLI UI with intercepted HTTP; no radio traffic."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
state = {'status': 'connected', 'host': 'test', 'port': 5000, 'channels': [],
         'contacts': {}, 'events': [], 'info': {}, 'device': {'fw ver': 14}, 'stats': {}}
requests = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def route(r):
        path = r.request.url.split('http://mesh.test/')[1]
        if path == 'api/companion/cli':
            requests.append(r.request.post_data_json)
            if len(requests) == 1:
                r.fulfill(json={'text': '<Companion>\nOK'})
            else:
                r.fulfill(status=502, json={'detail': 'Keine CLI-Antwort.'})
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
    page.locator('#device-nav').click()
    page.locator('#device-companion-nav').click()
    expect(page.locator('#companion-command')).to_be_visible()
    expect(page.locator('#companion-send')).to_be_disabled()
    page.locator('#companion-command').fill('get name')
    page.locator('#companion-reference-nav').click()
    expect(page.locator('#companion-console')).to_be_hidden()
    expect(page.locator('#companion-reference-nav')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#companion-command-list')).to_contain_text('set wifi.ssid <ssid>')
    expect(page.locator('#companion-command-list')).not_to_contain_text('password <new_password>')
    page.locator('#companion-command-search').fill('WIFI')
    expect(page.locator('#companion-command-list tr')).to_have_count(6)
    page.locator('#companion-command-search').fill('kein-solches-kommando')
    expect(page.locator('#companion-command-empty')).to_be_visible()
    page.locator('#companion-command-search').fill('')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert requests == []
    page.locator('#companion-reference-nav').press('ArrowLeft')
    expect(page.locator('#companion-console-nav')).to_be_focused()
    expect(page.locator('#companion-command')).to_have_value('get name')
    page.locator('#companion-console-nav').press('End')
    expect(page.locator('#companion-reference')).to_be_visible()
    page.locator('#companion-reference-nav').press('Home')
    expect(page.locator('#companion-console')).to_be_visible()
    page.locator('#companion-command').fill('get name')
    page.locator('#companion-command').press('Enter')
    expect(page.locator('#companion-replies')).to_have_text('<Companion>\nOK\n')
    expect(page.locator('#companion-command')).to_have_value('')
    assert requests == [{'command': 'get name'}]
    command = page.locator('#companion-command')
    command.fill('get radio')
    command.press('ArrowUp')
    expect(command).to_have_value('get name')
    command.press('ArrowUp')
    expect(command).to_have_value('get name')
    command.press('ArrowDown')
    expect(command).to_have_value('get radio')
    page.locator('#companion-reference-nav').click()
    page.locator('#companion-console-nav').click()
    expect(page.locator('#companion-replies')).to_have_text('<Companion>\nOK\n')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#companion-command').fill('ä' * 81)
    expect(page.locator('#companion-send')).to_be_disabled()
    page.locator('#companion-command').fill('reboot')
    page.locator('#companion-send').click()
    expect(page.locator('#companion-replies')).to_contain_text('Keine CLI-Antwort.')
    expect(command).to_be_enabled()
    command.press('ArrowUp')
    expect(command).to_have_value('reboot')
    command.press('ArrowUp')
    expect(command).to_have_value('get name')
    command.press('ArrowDown')
    expect(command).to_have_value('reboot')
    command.press('ArrowDown')
    expect(command).to_have_value('')
    command.press('ArrowUp')
    command.press('Enter')
    expect(command).to_be_enabled()
    expect(command).to_have_value('')
    command.press('ArrowUp')
    command.press('ArrowUp')
    expect(command).to_have_value('get name')  # Consecutive duplicates skipped.
    state['info']['public_key'] = 'ab' * 32
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    command.press('ArrowUp')
    expect(command).to_have_value('')
    del state['info']['public_key']
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    command.press('ArrowUp')
    expect(command).to_have_value('reboot')
    page.locator('#companion-cli-clear').click()
    expect(page.locator('#companion-replies')).to_be_empty()
    state['device']['fw ver'] = 13
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#companion-command')).to_be_disabled()
    expect(page.locator('#companion-cli-status')).to_contain_text('Firmware 1.18')
    state['status'] = 'offline'
    page.evaluate('(data)=>testStream.listeners.state({data:JSON.stringify(data)})', state)
    expect(page.locator('#companion-cli-status')).to_have_text('Companion nicht verbunden.')
    assert not errors, errors
    browser.close()
print('Companion CLI browser checks passed')
