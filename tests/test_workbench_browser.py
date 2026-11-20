"""Exercise the shipped workbench in Edge using isolated, synthetic task data."""
import json
import os
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@unittest.skipUnless(os.environ.get('CINEMA_BROWSER_TEST') == '1', 'opt-in browser smoke')
class WorkbenchBrowser(unittest.TestCase):
    def test_responsive_workbench_and_manual_match(self):
        from playwright.sync_api import sync_playwright
        root = Path(__file__).resolve().parents[1] / 'src/personal_cinema/legacy'
        fixture = {
            'cache': {'phase': 'idle', 'cache': {'bytes': 120000000000, 'limit': 400000000000, 'ready_items': 12}},
            'items': [{'id': 'demo-one', 'item_id': 'demo-one', 'title': '示例电影 · 一段很长的中文影片名称用于检查布局',
                       'source': '本地媒体', 'kind': 'Movies', 'size': 32000000000, 'phase': 'ready',
                       'bytes': 32000000000, 'percent': 100, 'poster': False}],
            'ingestion': [{'id': 'demo-two', 'title': '示例剧集', 'source': '已连接来源', 'files': 8,
                           'state': 'review', 'steps': [{'title': label, 'state': state} for label, state in
                           [('发现文件', 'done'), ('分类整理', 'done'), ('匹配作品', 'review'),
                            ('海报与简介', 'waiting'), ('中文演职人员', 'waiting'), ('演职员头像', 'waiting')]],
                           'note': '可以选择候选作品，或粘贴百科链接完成匹配。'}],
            'automation': {'manual_only': True, 'batch': {'state': 'complete'}},
            'capabilities': {'open_settings': True},
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                files = {'/MediaCache/': ('task-center.html', 'text/html'),
                         '/MediaCache/app.css': ('task-center.css', 'text/css'),
                         '/MediaCache/app.js': ('task-center.js', 'text/javascript'),
                         '/MediaCache/manual-match.js': ('task-manual-match.js', 'text/javascript')}
                if self.path in files:
                    name, kind = files[self.path]
                    body = (root / name).read_bytes()
                elif self.path == '/MediaCache/Tasks':
                    kind, body = 'application/json', json.dumps(fixture).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', kind + '; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1050})
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                page.add_init_script("sessionStorage.setItem('ml-console-token', 'synthetic-test-token')")
                page.goto(f'http://127.0.0.1:{server.server_port}/MediaCache/')
                page.locator('#connection').filter(has_text='已连接').wait_for()
                page.locator('.media-row').wait_for()
                self.assertEqual(page.locator('.brand').inner_text(), 'LumaNest工作台')
                for width in (1440, 768, 390, 320):
                    page.set_viewport_size({'width': width, 'height': 1050})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    box = page.locator('.mast').bounding_box()
                    heading = page.locator('.heading').bounding_box()
                    self.assertLessEqual(box['y'] + box['height'], heading['y'] + 1)
                    page.locator('#ingest-tab').click()
                    page.locator('.manual-match-button').click()
                    page.locator('.mm-dialog').wait_for()
                    self.assertLessEqual(page.locator('.mm-dialog').evaluate('(n)=>n.scrollWidth-n.clientWidth'), 1)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    page.keyboard.press('Escape')
                    page.locator('.mm-dialog').wait_for(state='hidden')
                    page.locator('#cache-tab').click()
                page.locator('.select-control input').check()
                page.locator('#refresh').click()
                self.assertTrue(page.locator('.select-control input').is_checked())
                page.emulate_media(reduced_motion='reduce')
                self.assertEqual(page.locator('.primary').first.evaluate('(n)=>getComputedStyle(n).transitionDuration'), '0s')
                self.assertEqual(errors, [])
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
