"""Choose a category and open a conversation through the visible navigation."""
def choose_conversation(page, group, name=None, target=None):
    back = page.locator('#conversation-back')
    if back.is_visible():
        back.click()
    page.locator('#conversation-kind').select_option({'channels': 'channel', 'contacts': 'dm', 'rooms': 'room'}[group])
    buttons = page.locator(f'#{group} button')
    if name is not None:
        buttons = buttons.filter(has_text=name)
    if target is not None:
        buttons = page.locator(f'#{group} button[data-target="{target}"]')
    buttons.first.click()
