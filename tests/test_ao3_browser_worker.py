import json
import socket
import unittest
from unittest.mock import Mock, patch

from browser.server import Handler, download
from browser.transport import allowed_url, connect_target
from src.sources.safe_http import UnsafeRequestError


class BrowserWorkerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('browser.server.signal.SIGKILL', 9, create=True))

    def test_connect_accepts_only_exact_https_hosts_and_pins_validated_address(self):
        for target in ('127.0.0.1:443', 'archiveofourown.org:80', 'archiveofourown.org.evil.test:443'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                connect_target(target)
        with patch('src.sources.safe_http.socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]), \
                self.assertRaises(UnsafeRequestError):
            connect_target('archiveofourown.org:443')
        connection = Mock()
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))
        with patch('browser.transport.resolve_addresses', return_value=[address]), \
                patch('browser.transport.socket.socket', return_value=connection):
            self.assertIs(connect_target('archiveofourown.org:443'), connection)
        connection.connect.assert_called_once_with(('1.1.1.1', 443))

    def test_route_urls_reject_credentials_private_targets_and_non_https(self):
        for url in ('http://archiveofourown.org', 'https://archiveofourown.org@evil.test',
                    'https://127.0.0.1/private', 'https://archiveofourown.org:444/work',
                    'https://archiveofourown.org\\evil.test', 'https://example.com'):
            self.assertFalse(allowed_url(url), url)
        self.assertTrue(allowed_url('https://archiveofourown.org/works/123?view_adult=true'))

    def test_api_cannot_request_arbitrary_urls_or_commands(self):
        handler = object.__new__(Handler)
        handler.respond = Mock()
        with patch('browser.server.download') as browser:
            for path in ('/works/1;rm', '/works/0', '/works/123?url=http://localhost',
                         '/https://archiveofourown.org', '/works/123/../../private'):
                handler.path = path
                handler.do_GET()
                self.assertEqual(handler.respond.call_args.args[0], 404)
        browser.assert_not_called()

    def test_busy_worker_does_not_start_another_browser(self):
        handler = object.__new__(Handler)
        handler.path = '/works/123'
        handler.respond = Mock()
        with patch('browser.server.SLOT') as slot, patch('browser.server.download') as browser:
            slot.acquire.return_value = False
            handler.do_GET()
            browser.assert_not_called()
            self.assertEqual(handler.respond.call_args.args[0], 503)

    def test_timed_out_job_restarts_container_to_reap_detached_browser_processes(self):
        import subprocess
        handler = object.__new__(Handler)
        handler.path = '/works/123'
        handler.respond = Mock()
        handler.wfile = Mock()
        with patch('browser.server.download', side_effect=subprocess.TimeoutExpired('browser', 165)), \
                patch('browser.server.os._exit') as restart:
            handler.do_GET()
        self.assertEqual(handler.respond.call_args.args[0], 502)
        restart.assert_called_once_with(1)

    def test_job_has_clean_environment_and_kills_browser_process_group(self):
        import subprocess
        def start(command, **kwargs):
            self.assertEqual(command[-1], '123')
            self.assertTrue(kwargs['start_new_session'])
            self.assertNotIn('BOT_TOKEN', kwargs['env'])
            self.assertNotIn('DATABASE_URL', kwargs['env'])
            self.assertNotIn('HTTP_PROXY', kwargs['env'])
            kwargs['stdout'].write(json.dumps({'work_id': '123', 'page': '', 'download': ''}).encode())
            return Mock(pid=999, wait=Mock(return_value=0))
        with patch('browser.server.subprocess.Popen', side_effect=start), \
                patch('browser.server.os.killpg', create=True) as kill:
            self.assertEqual(json.loads(download('123'))['work_id'], '123')
            kill.assert_called_once()
        process = Mock(pid=999, wait=Mock(side_effect=[subprocess.TimeoutExpired('browser', 165), 0]))
        with patch('browser.server.subprocess.Popen', return_value=process), \
                patch('browser.server.os.killpg', create=True) as kill, self.assertRaises(subprocess.TimeoutExpired):
            download('123')
        kill.assert_called_once()
