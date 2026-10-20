from dataclasses import replace
from io import BytesIO
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from src.core.limits import ResourceLimitError, ResourceLimits, resource_scope
from src.sources.collections import CollectionError, _CollectionRedirectHandler, load_collection
from src.sources.registry import is_ficbook_collection_url, normalize_collection_url
from src.sources.share_links import extract_download_url


URL = 'https://ficbook.net/collections/0196c5fa-9acb-7103-9374-2de5b5297e0c'


def page(*ids, pages=(), extra=''):
    works = ''.join(f'<article><div class="fanfic-inline-title"><a class="visit-link" '
                    f'href="/readfic/{id}">Book {id}</a></div></article>' for id in ids)
    links = ''.join(f'<a href="{href}">{i}</a>' for i, href in enumerate(pages))
    return f'<html><body><main><h1>My collection</h1>{works}{extra}' \
           f'<nav class="page-pagination">{links}</nav></main></body></html>'


def response(html, url=URL):
    result = BytesIO(html.encode())
    result.headers = {'Content-Type': 'text/html; charset=utf-8'}
    result.geturl = lambda: url
    return result


class CollectionLinkTests(unittest.TestCase):
    def test_collection_links_are_recognized_without_becoming_story_links(self):
        for url in (URL, URL + '?p=2', URL + '/page/2', 'http://www.ficbook.com/collections/123/'):
            self.assertTrue(is_ficbook_collection_url(url), url)
            self.assertIsNotNone(extract_download_url('Download ' + url))
        self.assertEqual(normalize_collection_url(URL + '?p=3#works'), URL)

    def test_untrusted_hosts_paths_and_credentials_are_rejected(self):
        for url in ('https://ficbook.net.evil.test/collections/123',
                    'https://user@ficbook.net/collections/123',
                    'https://ficbook.net:8080/collections/123',
                    'https://ficbook.net/collections/123/edit',
                    'https://ficbook.net/collections/../authors/1',
                    'https://ficbook.net/collections/123\\evil',
                    'https://ficbook.net/collections/invalid-id'):
            self.assertFalse(is_ficbook_collection_url(url), url)


class CollectionLoaderTests(unittest.TestCase):
    def load(self, pages, url=URL, **kwargs):
        opener = Mock()
        def fetch(request, **options):
            self.assertNotIn('Cookie', request.headers)
            self.assertNotIn('Authorization', request.headers)
            return response(pages[request.full_url], request.full_url)
        opener.open.side_effect = fetch
        with patch('src.sources.collections.build_safe_opener', return_value=opener):
            result = load_collection(url, **kwargs)
        return result, opener

    def test_all_pages_are_sorted_and_duplicates_removed(self):
        result, opener = self.load({
            URL: page(1, 2, pages=('?p=3', '?p=2')),
            URL + '?p=2': page(2, 3, pages=('?p=1', '?p=3')),
            URL + '?p=3': page(4, pages=('?p=2',)),
        }, url=URL + '?p=3')
        self.assertEqual(result.title, 'My collection')
        self.assertEqual(result.urls, tuple(f'https://ficbook.net/readfic/{i}' for i in (1, 2, 3, 4)))
        self.assertEqual(opener.open.call_count, 3)

    def test_only_work_titles_not_recommendations_or_foreign_urls_are_collected(self):
        extra = ('<aside><div class="fanfic-inline-title"><a class="visit-link" href="/readfic/99">Ad</a></div></aside>'
                 '<a href="/readfic/88">Description link</a>'
                 '<div class="fanfic-inline-title"><a class="visit-link" '
                 'href="https://evil.test/readfic/77">Foreign</a></div>')
        result, _ = self.load({URL: page(1, '1/123#part', extra=extra)})
        self.assertEqual(result.urls, ('https://ficbook.net/readfic/1',))

    def test_explicit_empty_collection_is_distinct_from_changed_markup(self):
        result, _ = self.load({URL: page(extra='<p>В сборнике пока нет фанфиков.</p>')})
        self.assertEqual(result.urls, ())
        for html in ('<html><h1>Sign in</h1></html>', page(), '<html>Cloudflare challenge</html>'):
            with self.subTest(html=html), self.assertRaises(CollectionError):
                self.load({URL: html})

    def test_broken_later_page_does_not_return_a_partial_collection(self):
        with self.assertRaises(CollectionError):
            self.load({URL: page(1, pages=('?p=2',)), URL + '?p=2': '<html>challenge</html>'})

    def test_other_collections_and_foreign_pagination_are_never_requested(self):
        result, opener = self.load({URL: page(1, pages=(
            '/collections/999?p=2', 'https://127.0.0.1/collections/123?p=2',
            'https://evil.test/collections/123?p=2', '?sort=popular',
        ))})
        self.assertEqual(len(result.urls), 1)
        opener.open.assert_called_once()

    def test_unrecognized_pagination_never_silently_returns_only_page_one(self):
        with self.assertRaises(CollectionError):
            self.load({URL: page(1, pages=('?p=2&unknown_filter=x',))})

    def test_path_pagination_and_absolute_links(self):
        result, _ = self.load({URL: page(1, pages=(URL + '/page/2',)),
                              URL + '/page/2': page(2)})
        self.assertEqual(len(result.urls), 2)

    def test_links_discovered_late_still_preserve_page_order(self):
        result, _ = self.load({URL: page(1, pages=('?p=3',)),
                              URL + '?p=3': page(3, pages=('?p=2',)),
                              URL + '?p=2': page(2)})
        self.assertEqual(result.urls, tuple(f'https://ficbook.net/readfic/{i}' for i in (1, 2, 3)))

    def test_work_fragment_is_removed_without_losing_the_work(self):
        result, _ = self.load({URL: page('1#comments')})
        self.assertEqual(result.urls, ('https://ficbook.net/readfic/1',))

    def test_private_page_is_rejected_even_with_work_links(self):
        with self.assertRaises(CollectionError):
            self.load({URL: page(1, extra='<p>Этот сборник закрыт.</p>')})

    def test_limits_fail_explicitly_instead_of_silently_truncating(self):
        limits = replace(ResourceLimits(), max_collection_works=1, max_collection_pages=1)
        with resource_scope(limits), self.assertRaises(ResourceLimitError):
            self.load({URL: page(1, 2)})
        with resource_scope(limits), self.assertRaises(ResourceLimitError):
            self.load({URL: page(1, pages=('?p=2',))})

    def test_forbidden_collection_does_not_try_bot_accounts(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(URL, 403, 'Forbidden', {}, BytesIO())
        with patch('src.sources.collections.build_safe_opener', return_value=opener):
            with self.assertRaises(CollectionError):
                load_collection(URL)
        opener.open.assert_called_once()

    def test_temporary_source_failure_retries_only_the_failed_page(self):
        opener = Mock()
        opener.open.side_effect = [HTTPError(URL, 503, 'Unavailable', {}, BytesIO()), response(page(1))]
        with patch('src.sources.collections.build_safe_opener', return_value=opener), patch(
            'src.sources.collections.bounded_sleep',
        ) as sleep:
            result = load_collection(URL)
        self.assertEqual(len(result.urls), 1)
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once_with(8.0)

    def test_redirect_cannot_leave_the_collection_or_enter_authentication(self):
        handler = _CollectionRedirectHandler(URL)
        for target in ('https://ficbook.net/login', 'https://ficbook.net/collections/999',
                       'http://ficbook.net/collections/123', 'https://127.0.0.1/'):
            with self.subTest(target=target), self.assertRaises((ValueError, OSError)):
                handler.redirect_request(None, None, 302, '', {}, target)

    def test_oversized_http_response_is_bounded(self):
        with resource_scope(replace(ResourceLimits(), max_response_bytes=5)):
            with self.assertRaises(ResourceLimitError):
                self.load({URL: page(1)})
