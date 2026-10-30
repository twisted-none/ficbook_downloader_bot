import unittest
from urllib.parse import urlsplit

from src.sources.registry import (
    extract_url, is_supported_story_url, normalize_url, normalize_collection_url, site_key,
)
from src.sources.share_links import extract_download_url


class RegistryLinkTests(unittest.TestCase):
    def test_extra_parameters_are_discarded_for_every_platform(self):
        cases = (
            ('https://ficbook.net/readfic/123/456', 'https://ficbook.net/readfic/123'),
            ('https://fikbooks.ru/readfic/405475', 'https://fikbooks.ru/readfic/405475'),
            ('https://fikbooks.ru/readfic/405475/123', 'https://fikbooks.ru/readfic/405475'),
            ('https://archiveofourown.org/works/123/chapters/456',
             'https://archiveofourown.org/works/123/chapters/456'),
            ('https://www.wattpad.com/story/123-name', 'https://www.wattpad.com/story/123-name'),
            ('https://www.wattpad.com/123-chapter', 'https://www.wattpad.com/123-chapter'),
            ('https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123',
             'https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123'),
            ('https://litnet.com/ru/book/title-b123', 'https://litnet.com/ru/reader/title-b123'),
            ('https://ranobelib.me/ru/book/123--title', 'https://ranobelib.me/ru/123--title'),
            ('https://zahleb.me/story/title-lBH5frx0bi/8fidkonOwl',
             'https://zahleb.me/story/title-lBH5frx0bi'),
        )
        for original, expected in cases:
            for query in ('ysclid=mvu3nhydj966232117', 'utm_source=search&arbitrary=1',
                          'next=http%3A%2F%2F127.0.0.1&url=https%3A%2F%2Fevil.example'):
                link = original + ('&' if '?' in original else '?') + query + '#part_content'
                with self.subTest(link=link):
                    self.assertTrue(is_supported_story_url(link))
                    self.assertEqual(extract_url('Book ' + link), link)
                    self.assertEqual(extract_download_url(link), link)
                    self.assertEqual(normalize_url(link), expected)
                    self.assertEqual(normalize_url(normalize_url(link)), expected)

    def test_necessary_ao3_flags_are_preserved(self):
        self.assertEqual(normalize_url('https://archiveofourown.org/works/123?'
            'ysclid=abc&view_adult=true&view_full_work=true#chapter'),
            'https://archiveofourown.org/works/123?view_adult=true&view_full_work=true')

    def test_host_case_and_default_port_do_not_change_platform(self):
        for original in ('https://ficbook.net/readfic/123', 'https://fikbooks.ru/readfic/123',
                         'https://archiveofourown.org/works/123', 'https://www.wattpad.com/story/123-title',
                         'https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123',
                         'https://litnet.com/ru/book/title-b123', 'https://ranobelib.me/ru/123--title',
                         'https://zahleb.me/story/title-lBH5frx0bi'):
            host = urlsplit(original).netloc
            link = original.replace(host, host.upper() + ':443')
            with self.subTest(link=link):
                self.assertTrue(is_supported_story_url(link))
                self.assertEqual(normalize_url(link), normalize_url(original))
                self.assertEqual(site_key(link), site_key(original))

    def test_collection_parameters_are_discarded(self):
        link = 'https://www.ficbook.net/collections/123?ysclid=abc&next=http://127.0.0.1#works'
        self.assertEqual(extract_download_url(link), link)
        self.assertEqual(normalize_collection_url(link), 'https://ficbook.net/collections/123')

    def test_only_exact_supported_hosts_and_regular_ports_are_allowed(self):
        for root in ('https://ficbook.net/readfic/123', 'https://fikbooks.ru/readfic/123',
                     'https://archiveofourown.org/works/123', 'https://www.wattpad.com/story/123-title',
                     'https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=123',
                     'https://litnet.com/ru/book/title-b123', 'https://ranobelib.me/ru/123--title',
                     'https://zahleb.me/story/title-lBH5frx0bi'):
            host = urlsplit(root).netloc
            for bad in (root.replace(host, host + '.evil.example'), root.replace(host, 'user@' + host),
                        root.replace(host, host + ':444'), root.replace(host, host + ':invalid'),
                        root.replace('https:', 'ftp:'), root + '\\bad', root + '\x00'):
                with self.subTest(link=bad):
                    self.assertFalse(is_supported_story_url(bad))
                    self.assertIsNone(extract_download_url(bad))

    def test_homepages_and_profiles_are_not_works(self):
        for host in ('ficbook.net', 'fikbooks.ru', 'archiveofourown.org', 'wattpad.com',
                     'hogwartsnet.ru', 'litnet.com', 'ranobelib.me', 'zahleb.me'):
            for path in ('/', '/users/123', '/authors/123'):
                with self.subTest(host=host, path=path):
                    self.assertFalse(is_supported_story_url(f'https://{host}{path}?ysclid=abc'))
