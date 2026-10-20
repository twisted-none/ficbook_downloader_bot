from io import BytesIO
import gzip
import brotli
import socket
import unittest
from unittest.mock import Mock, patch

import requests
from fanficfare.fetchers import RequestsFetcher
from urllib3 import PoolManager
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.response import HTTPResponse

from src.core.limits import ResourceLimitError, ResourceLimits, resource_scope
from dataclasses import replace
from src.sources.ficbook import FicbookAccount, FicbookClient
from src.sources.safe_http import UnsafeRequestError


URL = 'https://archiveofourown.org/works/82620966/navigate?view_adult=true'


class AO3RequestsTransportTests(unittest.TestCase):
    def test_session_only_advertises_bounded_supported_decoders(self):
        from src.sources.ao3_http import AO3Session
        session = AO3Session()
        self.addCleanup(session.close)
        self.assertEqual(session.headers['Accept-Encoding'], 'gzip, deflate, br')

    def test_ao3_uses_requests_without_changing_ficbook(self):
        client = FicbookClient()
        fetcher = client._build_config(URL, FicbookAccount()).get_fetcher()
        self.assertIsInstance(fetcher, RequestsFetcher)
        ficbook = client._build_config('https://ficbook.net/readfic/1', FicbookAccount()).get_fetcher()
        self.assertIs(type(ficbook), RequestsFetcher)

    def test_invalid_targets_and_unverified_tls_are_blocked_before_session_send(self):
        from src.sources.ao3_http import AO3Session
        session = AO3Session()
        self.addCleanup(session.close)
        with patch('requests.sessions.Session.send') as send:
            for url in ('https://127.0.0.1/a', 'https://evil.example/a',
                        'http://archiveofourown.org/a', 'https://user:dummy@archiveofourown.org/a'):
                with self.subTest(url=url), self.assertRaises(UnsafeRequestError):
                    session.send(requests.Request('GET', url).prepare())
            with self.assertRaises(UnsafeRequestError):
                session.send(requests.Request('GET', URL).prepare(), verify=False)
            with self.assertRaises(UnsafeRequestError):
                session.send(requests.Request('GET', URL).prepare(), proxies={'https': 'http://127.0.0.1:8080'})
            send.assert_not_called()

    def test_redirect_validation_happens_before_requests_normalizes_location(self):
        from src.sources.ao3_http import AO3Session
        session = AO3Session()
        self.addCleanup(session.close)
        response = requests.Response()
        response.request = requests.Request('POST', URL, data={'password': 'dummy'}).prepare()
        response.url = URL
        response.status_code = 302
        for location in ('https://evil.example/', 'https://archiveofourown.org/\nlogin',
                         'http://archiveofourown.org/login', 'https://download.archiveofourown.org/login'):
            response.headers['Location'] = location
            with self.subTest(location=location), self.assertRaises(UnsafeRequestError):
                session.get_redirect_target(response)

    def test_connection_rejects_mixed_private_dns_before_opening_socket(self):
        from src.sources.ao3_http import PublicAO3Connection
        mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('104.18.1.1', 443)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch('src.sources.safe_http.socket.getaddrinfo', return_value=mixed), \
                patch('src.sources.ao3_http.socket.socket') as create:
            with self.assertRaises(UnsafeRequestError):
                PublicAO3Connection('archiveofourown.org', timeout=1)._new_conn()
        create.assert_not_called()

    def test_connection_pins_validated_ip_but_keeps_tls_hostname(self):
        from src.sources.ao3_http import PublicAO3Connection
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('104.18.1.1', 443))]
        with patch('src.sources.safe_http.socket.getaddrinfo', return_value=addresses), \
                patch('src.sources.ao3_http.socket.socket') as create:
            connection = PublicAO3Connection('archiveofourown.org', timeout=1)
            connection._new_conn()
        create.return_value.connect.assert_called_once_with(('104.18.1.1', 443))
        self.assertEqual(connection.host, 'archiveofourown.org')

    def test_adapter_bounds_body_and_does_not_modify_global_https_pool(self):
        from src.sources.ao3_http import AO3Adapter
        adapter = AO3Adapter()
        self.addCleanup(adapter.close)
        self.assertIs(PoolManager().pool_classes_by_scheme['https'], HTTPSConnectionPool)
        response = requests.Response()
        response.status_code = 200
        response.raw = HTTPResponse(body=BytesIO(b'x' * 20), headers={}, preload_content=False)
        with patch('requests.adapters.HTTPAdapter.send', return_value=response), \
                resource_scope(replace(ResourceLimits(), max_response_bytes=10)):
            with self.assertRaises(ResourceLimitError):
                adapter.send(requests.Request('GET', URL).prepare())
        self.assertTrue(response.raw.closed)

    def test_brotli_and_gzip_are_decoded_with_a_limit_on_uncompressed_data(self):
        from src.sources.ao3_http import AO3Adapter
        for encoding, compress in (('gzip', gzip.compress), ('br', brotli.compress)):
            with self.subTest(encoding=encoding):
                adapter = AO3Adapter()
                self.addCleanup(adapter.close)
                response = requests.Response()
                response.status_code = 200
                response.raw = HTTPResponse(body=BytesIO(compress(b'x' * 10000)), preload_content=False,
                                            headers={'Content-Encoding': encoding})
                with patch('requests.adapters.HTTPAdapter.send', return_value=response), \
                        resource_scope(replace(ResourceLimits(), max_response_bytes=1024)):
                    with self.assertRaises(ResourceLimitError):
                        adapter.send(requests.Request('GET', URL).prepare())
                self.assertTrue(response.raw.closed)

    def test_compressed_response_is_returned_decoded_like_native_requests(self):
        from src.sources.ao3_http import AO3Adapter
        adapter = AO3Adapter()
        self.addCleanup(adapter.close)
        body = brotli.compress(b'<html>AO3 story</html>')
        response = requests.Response()
        response.status_code = 200
        response.raw = HTTPResponse(body=BytesIO(body), preload_content=False,
                                    headers={'Content-Encoding': 'br', 'Content-Length': str(len(body))})
        with patch('requests.adapters.HTTPAdapter.send', return_value=response):
            result = adapter.send(requests.Request('GET', URL).prepare())
        self.assertEqual(result.content, b'<html>AO3 story</html>')

    def test_redirect_body_is_not_read(self):
        from src.sources.ao3_http import AO3Adapter
        adapter = AO3Adapter()
        self.addCleanup(adapter.close)
        response = requests.Response()
        response.status_code = 302
        response.headers['Location'] = '/works/82620966'
        response.raw = Mock(read1=Mock(side_effect=AssertionError('Redirect body read')))
        with patch('requests.adapters.HTTPAdapter.send', return_value=response):
            result = adapter.send(requests.Request('GET', URL).prepare())
        self.assertEqual(result.content, b'')
        response.raw.read1.assert_not_called()
        response.raw.close.assert_called_once()

    def test_credentials_are_stripped_on_cross_origin_get_redirects(self):
        from src.sources.ao3_http import AO3Session
        session = AO3Session()
        self.addCleanup(session.close)
        response = requests.Response()
        response.request = requests.Request('GET', URL).prepare()
        prepared = requests.Request('GET', 'https://download.archiveofourown.org/downloads/82620966/Book.html',
                                    headers={'Cookie': 'private=dummy', 'Authorization': 'Bearer dummy',
                                             'Referer': URL}).prepare()
        session.rebuild_auth(prepared, response)
        self.assertTrue({'Cookie', 'Authorization', 'Referer'}.isdisjoint(prepared.headers))
