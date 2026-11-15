import importlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.services.intranet import DEFAULT_CONFIG, browser_config, save_config


class NetworkTests(unittest.TestCase):
    def test_protocol_config_validation_and_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.json').write_text('{"other": "preserved"}', encoding='utf8')
            values = save_config(root, {'https_auto_http_enabled': True, 'https_redirect_http_port': 7860,
                'intranet_auto_redirect_enabled': True, 'intranet_redirect_host': '192.168.1.10'}, {})
            self.assertEqual(values['https_redirect_http_port'], 7860)
            self.assertEqual(json.loads((root / 'config.json').read_text())['other'], 'preserved')
            self.assertEqual(browser_config(root, {}), {'origin': 'http://192.168.1.10:7777', 'auto': True, 'http_enabled': True, 'http_port': 7860})
            for changes in ({'https_redirect_http_port': 0}, {'https_redirect_http_port': True}, {'https_auto_http_enabled': 'true'}):
                with self.assertRaises(ValueError):
                    save_config(root, changes, {})
            self.assertFalse(DEFAULT_CONFIG['https_auto_http_enabled'])

    def test_network_templates_health_and_cookie_routing(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'APP_STORAGE_ROOT': directory}):
            main = importlib.import_module('app.main')
            root = Path(directory)
            with patch.object(main, 'config', replace(main.config, config_dir=root)), patch.object(main.db, 'get_settings', return_value={}):
                save_config(root, {'intranet_auto_redirect_enabled': True, 'intranet_redirect_host': '192.168.1.10'}, {})
                client = TestClient(main.app, base_url='https://gallery.example')
                page = client.get('/')
                self.assertEqual(page.status_code, 200)
                self.assertIn('data-intranet-preflight="1"', page.text)
                fallback = client.get('/?_gallery_skip_intranet=1')
                self.assertNotIn('data-intranet-preflight="1"', fallback.text)
                self.assertIn('aria-label="HTTPS 自动转 HTTP 说明"', fallback.text)
                client.cookies.set('gallery_intranet_checked', 'http%3A%2F%2F192.168.1.10%3A7777')
                self.assertNotIn('data-intranet-preflight="1"', client.get('/').text)
                self.assertIn('/static/intranet.js?', client.get('/').text)
                save_config(root, {'https_auto_http_enabled': True}, {})
                self.assertIn('data-intranet-preflight="1"', client.get('/').text)
                for endpoint in ['/intranet/health', '/intranet/health.gif']:
                    response = client.get(endpoint)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual(response.headers['access-control-allow-origin'], '*')
                    self.assertEqual(client.options(endpoint).status_code, 204)
                self.assertTrue(client.get('/intranet/health.gif').content.startswith(b'GIF89a'))
                self.assertNotIn('access-control-allow-origin', fallback.headers)


if __name__ == '__main__':
    unittest.main()
