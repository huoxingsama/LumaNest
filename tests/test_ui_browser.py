"""Real local HTTP/DOM smoke test, isolated from the user's services and media.

Run explicitly with a Playwright installation and Microsoft Edge. The normal
unittest suite skips it unless CINEMA_BROWSER_TEST=1 is set.
"""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit


@unittest.skipUnless(os.environ.get('CINEMA_BROWSER_TEST') == '1', 'opt-in browser smoke')
class SettingsBrowser(unittest.TestCase):
    def submit_credential_modal(self, page):
        """Capture the failing boundary without logging credential values/headers."""
        from playwright.sync_api import TimeoutError as BrowserTimeout
        events = []
        tracked = {'/api/credentials', '/api/status'}

        def request_started(request):
            path = urlsplit(request.url).path
            if path in tracked:
                events.append(('request', request.method, path))

        def response_received(response):
            path = urlsplit(response.url).path
            if path in tracked:
                events.append(('response', response.request.method, path, response.status))

        def request_failed(request):
            path = urlsplit(request.url).path
            if path in tracked:
                events.append(('failed', request.method, path, request.failure))

        def details():
            validation = page.locator('#modal-form').evaluate(
                """form => Array.from(form.elements).filter(el => el.validity && !el.validity.valid)
                    .map(el => ({name: el.name, message: el.validationMessage}))""")
            return f'HTTP events={events!r}; validation={validation!r}; modal error={page.locator("#modal-error").inner_text()!r}'

        listeners = [('request', request_started), ('response', response_received),
                     ('requestfailed', request_failed)]
        for name, callback in listeners:
            page.on(name, callback)
        try:
            self.assertTrue(page.locator('#modal-form').evaluate('form => form.checkValidity()'), details())
            page.locator('#modal [type="submit"]').click()
            try:
                # Surface a real application error immediately instead of only
                # reporting that the still-open dialog exceeded 30 seconds.
                page.locator('#modal:not([open]), #modal-error:not([hidden])').first.wait_for(state='attached')
            except BrowserTimeout:
                self.fail('Credential submission did not finish: ' + details())
            self.assertFalse(page.locator('#modal').is_visible(), details())
            self.assertIn(('response', 'POST', '/api/credentials', 200), events, details())
        finally:
            for name, callback in listeners:
                page.remove_listener(name, callback)

    def test_local_settings_and_responsive_forms(self):
        from personal_cinema.server import Application, handler
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory(prefix='cinema-ui-test-') as folder:
            app = Application(Path(folder))
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='msedge', headless=True)
                    page = browser.new_page(viewport={'width':1440, 'height':1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/#token={app.launch_token}')
                    page.locator('#connection').filter(has_text='已连接').wait_for()
                    self.assertNotIn('token=', page.url)
                    self.assertEqual(page.locator('.nav-button').count(), 12)

                    # Built-in playback is the normal setup; an executable is
                    # only requested when the user explicitly chooses external.
                    self.assertEqual(page.locator('[data-key="foundation.mpv_backend"]').input_value(), 'embedded')
                    self.assertEqual(page.locator('[data-key="foundation.mpv_path"]').count(), 0)
                    page.locator('[data-key="foundation.mpv_backend"]').select_option('external')
                    page.locator('[data-key="foundation.mpv_path"]').fill('C:/Example/MPV/mpv.exe')
                    page.locator('[data-key="foundation.mpv_backend"]').select_option('embedded')
                    self.assertEqual(page.locator('[data-key="foundation.mpv_path"]').count(), 0)
                    with page.expect_response(lambda response: '/api/config?scope=' in response.url):
                        page.locator('[data-action="discard"]').click()

                    page.locator('[data-section="cache"]').click()
                    buffer = page.locator('[data-key="cache.playback_gib"]')
                    buffer.fill('')
                    self.assertTrue(page.locator('#save-button').is_disabled())
                    buffer.fill('12')
                    page.locator('#save-button').click()
                    page.locator('#save-count').filter(has_text='已与本机同步').wait_for()
                    self.assertEqual(app.config.read()['values']['cache.playback_gib'], 12)

                    page.locator('[data-section="sources"]').click()
                    page.locator('[data-action="source-add"]').click()
                    page.locator('#modal [name="id"]').fill('sample')
                    page.locator('#modal [name="name"]').fill('测试电影 <img src=x>')
                    page.locator('#modal [name="kind"]').select_option('local')
                    self.assertTrue(page.locator('#modal [name="allow_write"]').is_disabled())
                    library = Path(folder) / 'library'
                    library.mkdir()
                    page.locator('#modal [name="path"]').fill(str(library))
                    page.locator('#modal [type="submit"]').click()
                    page.locator('#modal').wait_for(state='hidden')
                    self.assertEqual(page.locator('.source-row img').count(), 0)
                    page.locator('#save-button').click()
                    page.locator('#save-count').filter(has_text='已与本机同步').wait_for()
                    entry = app.config.read()['values']['sources.entries'][0]
                    self.assertEqual(entry['id'], 'sample')
                    self.assertFalse(entry['allow_write'])

                    page.locator('[data-section="maintenance"]').click()
                    page.locator('[data-action="credential-add"]').click()
                    page.locator('#modal [name="name"]').fill('test.token')
                    page.locator('#modal [name="value"]').fill('synthetic-ui-test-secret')
                    self.submit_credential_modal(page)
                    self.assertEqual(app.secrets.get('test.token'), 'synthetic-ui-test-secret')
                    self.assertNotIn('synthetic-ui-test-secret', page.locator('body').inner_text())

                    page.locator('#scope').select_option('device')
                    page.locator('#search').fill('缓冲')
                    page.locator('[data-key="cache.playback_gib"]').wait_for()
                    page.locator('[data-key="cache.playback_gib"]').fill('14')
                    page.locator('#save-button').click()
                    page.locator('#save-count').filter(has_text='已与本机同步').wait_for()
                    self.assertEqual(app.config.get_effective('device')['cache.playback_gib'], 14)
                    self.assertEqual(app.config.read()['values']['cache.playback_gib'], 12)
                    page.locator('[data-reset-key="cache.playback_gib"]').click()
                    page.locator('#save-button').click()
                    page.locator('#save-count').filter(has_text='已与本机同步').wait_for()
                    self.assertEqual(app.config.get_effective('device')['cache.playback_gib'], 12)

                    # A rejected scope change must leave both data and the selector
                    # on the current scope; drafts must never be silently dropped.
                    page.locator('[data-key="cache.playback_gib"]').fill('15')
                    page.locator('#scope').select_option('global')
                    page.locator('#status-message').filter(has_text='先保存或撤销').wait_for()
                    self.assertEqual(page.locator('#scope').input_value(), 'device')
                    self.assertEqual(page.locator('[data-key="cache.playback_gib"]').input_value(), '15')
                    page.locator('[data-action="discard"]').click()
                    page.locator('#save-count').filter(has_text='已与本机同步').wait_for()

                    # Managed OpenList offers its real first-initialization inputs.
                    page.locator('#scope').select_option('global')
                    page.locator('#search').fill('')
                    page.locator('[data-section="sources"]').click()
                    page.locator('[data-key="sources.openlist_mode"]').select_option('managed')
                    page.locator('#save-button').click()
                    page.locator('#save-count').filter(has_text='已与本机同步').wait_for()
                    page.locator('[data-service="openlist"][data-service-action="plan"]').click()
                    self.assertEqual(page.locator('#modal [name="username"]').count(), 1)
                    self.assertEqual(page.locator('#modal [name="password"]').count(), 1)
                    page.locator('#modal [data-action="close-modal"]').first.click()

                    page.locator('[data-action="source-add"]').click()
                    page.locator('#modal [name="id"]').fill('cloud_sample')
                    page.locator('#modal [name="name"]').fill('测试夸克来源')
                    page.locator('#modal [name="path"]').fill('/chosen/films')
                    page.locator('#modal [name="driver"]').select_option('Quark')
                    page.locator('#modal [name="mount_path"]').fill('/chosen')
                    page.locator('#modal [name="native_root"]').fill('folder7')
                    page.locator('#modal [name="allow_write"]').check()
                    page.locator('#modal [type="submit"]').click()
                    page.locator('#modal').wait_for(state='hidden')
                    page.locator('#save-button').click()
                    page.locator('#save-count').filter(has_text='已与本机同步').wait_for()
                    cloud = app.config.read()['values']['sources.entries'][1]
                    self.assertEqual(cloud['native_root'], 'folder7')
                    self.assertEqual(cloud['driver'], 'Quark')
                    page.locator('[data-section="organizing"]').click()
                    page.locator('[data-action="organizing-refresh"]').click()
                    page.locator('[data-organizing="plan"][data-source-id="cloud_sample"]').wait_for()
                    self.assertEqual(page.locator('[data-organizing="plan"]').count(), 1)

                    page.locator('[data-section="maintenance"]').click()
                    page.locator('[data-action="backup-create"]').click()
                    page.locator('[data-backup-restore]').wait_for()
                    backup_id = page.locator('[data-backup-restore]').first.get_attribute('data-backup-restore')
                    page.locator('[data-section="cache"]').click()
                    page.locator('[data-key="cache.playback_gib"]').fill('17')
                    page.locator('#save-button').click()
                    page.locator('#save-count').filter(has_text='已与本机同步').wait_for()
                    page.locator('[data-section="maintenance"]').click()
                    page.locator(f'[data-backup-restore="{backup_id}"]').click()
                    # A second writer prevents a stale restore; the modal shows
                    # the actual server conflict instead of claiming success.
                    current = app.config.read()
                    app.config.update({'cache.playback_gib': 18}, current['revision'])
                    page.locator('#modal [type="submit"]').click()
                    page.locator('#modal-error').filter(has_text='另一').wait_for()
                    self.assertEqual(app.config.read()['values']['cache.playback_gib'], 18)
                    page.locator('#modal [data-action="close-modal"]').first.click()
                    with page.expect_response(lambda response: '/api/config?scope=' in response.url):
                        page.locator('[data-action="discard"]').click()
                    page.locator(f'[data-backup-restore="{backup_id}"]').click()
                    page.locator('#modal [type="submit"]').click()
                    page.locator('#modal:not([open]), #modal-error:not([hidden])').first.wait_for(state='attached')
                    self.assertFalse(page.locator('#modal').is_visible(), page.locator('#modal-error').inner_text())
                    self.assertEqual(app.config.read()['values']['cache.playback_gib'], 12)

                    old_log = Path(folder) / 'runtime/logs/test-old.log'
                    old_log.parent.mkdir(parents=True, exist_ok=True)
                    old_log.write_text('synthetic log', encoding='utf-8')
                    os.utime(old_log, (time.time() - 40 * 86400,) * 2)
                    page.locator('[data-action="maintenance-prune"]').click()
                    page.locator('#modal [type="submit"]').click()
                    page.locator('#modal').wait_for(state='hidden')
                    self.assertFalse(old_log.exists())
                    page.locator('#status-message').filter(has_text='删除 1 项').wait_for()

                    page.set_viewport_size({'width':390,'height':844})
                    page.locator('#search').fill('')
                    # Every group must remain usable on a phone, including
                    # long source paths, credentials and recovery actions.
                    sections = page.locator('.nav-button').evaluate_all('(nodes) => nodes.map(node => node.dataset.section)')
                    for section in sections:
                        page.locator(f'[data-section="{section}"]').click()
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390, section)
                        outside = page.locator('main input:visible, main select:visible, main textarea:visible, #savebar button').evaluate_all('''nodes => nodes.filter(node => {
                            const box = node.getBoundingClientRect();
                            return box.left < 0 || box.right > window.innerWidth;
                        }).map(node => node.id || node.name)''')
                        self.assertEqual(outside, [], section)

                    page.locator('[data-section="sources"]').click()
                    page.locator('[data-action="source-add"]').click()
                    self.assertLessEqual(page.locator('#modal').evaluate('node => node.scrollWidth'), 358)
                    page.locator('#modal [data-action="close-modal"]').first.click()

                    page.set_viewport_size({'width':1440,'height':1000})
                    page.locator('[data-section="cache"]').click()
                    label_size = page.locator('.setting-label').first.evaluate('node => parseFloat(getComputedStyle(node).fontSize)')
                    self.assertGreaterEqual(label_size, 15)
                    self.assertGreaterEqual(page.locator('#save-button').bounding_box()['height'], 44)
                    # Hover stays subtle; keyboard users still get a clear focus ring.
                    page.locator('#refresh').hover()
                    self.assertEqual(page.locator('#refresh').evaluate('node => getComputedStyle(node).outlineStyle'), 'none')
                    page.locator('#search').focus()
                    page.keyboard.press('Tab')
                    self.assertEqual(page.locator(':focus-visible').get_attribute('id'), 'scope')
                    self.assertEqual(page.locator('#scope').evaluate('node => getComputedStyle(node).outlineWidth'), '2px')
                    self.assertEqual(page.locator('#scope').evaluate('node => getComputedStyle(node).outlineColor'), 'rgb(244, 241, 234)')
                    screenshot = os.environ.get('CINEMA_UI_SCREENSHOT')
                    if screenshot:
                        page.set_viewport_size({'width':1440,'height':1000})
                        page.locator('#scope').select_option('global')
                        page.locator('[data-section="foundation"]').click()
                        page.screenshot(path=screenshot, full_page=True)
                    self.assertEqual(errors, [])
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
