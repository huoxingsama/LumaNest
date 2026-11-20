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


@unittest.skipUnless(os.environ.get('CINEMA_BROWSER_TEST') == '1', 'opt-in browser smoke')
class SettingsBrowser(unittest.TestCase):
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
                    page.locator('#modal [type="submit"]').click()
                    page.locator('#modal').wait_for(state='hidden')
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
                    page.locator('[data-section="playback"]').click()
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
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
