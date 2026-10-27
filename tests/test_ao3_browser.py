import unittest
from unittest.mock import patch

import requests
from fanficfare.exceptions import HTTPErrorFFF

from src.core.limits import ResourceLimitError
from src.sources.ficbook import FicbookClient, FicbookError

URL = 'https://archiveofourown.org/works/123/chapters/456'
PAGE = '''<div id="main"><dl class="work meta"><dd class="chapters">2/2</dd>
<dd class="language">English</dd><dd class="fandom tags"><a>Example</a></dd></dl>
<div id="workskin"><div class="preface"><h2 class="title">Book &amp; title</h2>
<h3 class="byline"><a rel="author">Writer</a></h3></div><div id="chapters">
<div class="chapter" id="chapter-1"><h3 class="title">First</h3>
<div class="userstuff"><p>First text <a href="#note">note</a></p></div></div>
<div class="chapter" id="chapter-2"><h3 class="title">Second</h3>
<div class="userstuff"><p id="note">Second text</p></div></div></div></div></div>'''


class AO3BrowserTests(unittest.TestCase):
    def test_full_work_metadata_chapters_and_selection(self):
        from src.sources.ao3_browser import parse_browser_work
        story = parse_browser_work(URL, {'page': PAGE, 'download': ''}, {2})
        self.assertEqual(story.title, 'Book & title')
        self.assertEqual(story.author, 'Writer')
        self.assertEqual(len(story.chapters), 1)
        self.assertEqual(story.chapters[0].title, 'Second')
        self.assertIn('Second text', story.chapters[0].html)
        self.assertNotIn('First text', story.chapters[0].html)

    def test_missing_empty_chapters_and_wrong_work_are_rejected(self):
        from src.sources.ao3_browser import parse_browser_work, BrowserDownloadError
        for page in (PAGE.replace('2/2', '3/3'), PAGE.replace('Second text', ''),
                     PAGE.replace('id="workskin"', 'id="missing"')):
            with self.subTest(page=page[-90:]), self.assertRaises(BrowserDownloadError):
                parse_browser_work(URL, {'page': page, 'download': ''}, None)

    def test_single_chapter_without_role_article_and_active_html_is_cleaned(self):
        from src.sources.ao3_browser import parse_browser_work
        page = PAGE[:PAGE.index('<div id="chapters">')] + '''<div id="chapters">
<div class="userstuff"><p onclick="evil()">Text <a href="javascript:evil()">link</a>
<script>evil()</script><img src="http://127.0.0.1/private"></p></div></div></div></div>'''
        story = parse_browser_work(URL, {'page': page.replace('2/2', '1/1'), 'download': ''}, None)
        self.assertEqual(len(story.chapters), 1)
        for bad in ('onclick', 'javascript:', '<script', '<img'):
            self.assertNotIn(bad, story.chapters[0].html)

    def test_browser_is_only_used_after_eligible_ao3_failure(self):
        from src.core.models import Story
        error = FicbookError('transport failed')
        error.__cause__ = requests.Timeout()
        expected = Story('Book', 'Author', URL)
        with patch.dict('os.environ', {'AO3_BROWSER_URL': 'http://ao3-browser:8000'}), \
                patch.object(FicbookClient, '_download_once', side_effect=error), \
                patch('src.sources.ao3_browser.download_browser_work', return_value=expected) as fallback:
            self.assertIs(FicbookClient().download(URL), expected)
            fallback.assert_called_once()

    def test_denials_limits_and_other_sites_do_not_use_browser(self):
        for url, error in ((URL, HTTPErrorFFF(URL, 429, 'Rate limited')),
                           (URL, HTTPErrorFFF(URL, 403, 'Private')),
                           (URL, requests.exceptions.SSLError()),
                           ('https://ficbook.net/readfic/123', requests.Timeout()),
                           (URL, ResourceLimitError())):
            wrapped = FicbookError('failed')
            wrapped.__cause__ = error
            with self.subTest(url=url, error=type(error).__name__), \
                    patch.dict('os.environ', {'AO3_BROWSER_URL': 'http://ao3-browser:8000'}), \
                    patch.object(FicbookClient, '_download_once', side_effect=wrapped), \
                    patch('src.sources.ao3_browser.download_browser_work') as fallback, self.assertRaises(FicbookError):
                FicbookClient().download(url)
            fallback.assert_not_called()

    def test_official_html_keeps_notes_links_and_selected_chapters(self):
        from src.sources.ao3_browser import parse_browser_work, BrowserDownloadError
        html = '''<div id="chapters"><div class="meta group"><h2 class="heading">First</h2></div>
        <div class="userstuff"><p>One <a href="#note">note</a></p></div>
        <div class="meta group"><h2 class="heading">Second</h2></div>
        <div class="userstuff"><p id="note">Two</p></div></div>'''
        story = parse_browser_work(URL, {'page': PAGE, 'download': html}, {1})
        self.assertEqual(len(story.chapters), 1)
        self.assertIn('href="#note"', story.chapters[0].html)
        self.assertEqual(story.chapters[0].title, 'First')
        with self.assertRaises(BrowserDownloadError):
            parse_browser_work(URL, {'page': PAGE, 'download': html.replace('Two', '')}, {1})

    def test_rate_limit_on_fallback_stops_real_browser(self):
        from src.sources.ao3_browser import eligible
        error = requests.Timeout()
        error.ao3_failures = ('requests:Timeout', 'public:HTTP 429')
        self.assertFalse(eligible(error))

    def test_preview_recovers_without_changing_normal_download(self):
        from src.core.models import StoryPreview
        error = FicbookError('transport')
        error.__cause__ = HTTPErrorFFF(URL, 403, 'Cloudflare challenge-platform')
        expected = StoryPreview('Book', 'Writer', URL)
        with patch.dict('os.environ', {'AO3_BROWSER_URL': 'http://ao3-browser:8000'}), \
                patch.object(FicbookClient, '_preview_once', side_effect=error), \
                patch('src.sources.ao3_browser.download_browser_work', return_value=expected) as fallback:
            self.assertIs(FicbookClient().preview(URL), expected)
        self.assertTrue(fallback.call_args.kwargs['preview'])

    def test_service_accepts_only_fixed_endpoint_and_checks_work_identity(self):
        import io
        import json
        from unittest.mock import Mock
        from src.sources.ao3_browser import BrowserDownloadError, fetch_bundle
        for endpoint in ('http://169.254.169.254', 'http://ao3-browser:8000@evil.test',
                         'http://127.0.0.1:5432'):
            with patch.dict('os.environ', {'AO3_BROWSER_URL': endpoint}), self.assertRaises(BrowserDownloadError):
                fetch_bundle(URL)
        stream = io.BytesIO(json.dumps({'work_id': '999', 'page': PAGE, 'download': ''}).encode())
        with patch.dict('os.environ', {'AO3_BROWSER_URL': 'http://ao3-browser:8000'}), \
                patch('src.sources.ao3_browser.build_opener', return_value=Mock(open=Mock(return_value=stream))), \
                self.assertRaises(BrowserDownloadError):
            fetch_bundle(URL)

    def test_native_ficbook_success_never_calls_browser(self):
        from src.core.models import Story
        expected = Story('Book', 'Writer', 'https://ficbook.net/readfic/123')
        with patch.dict('os.environ', {'AO3_BROWSER_URL': 'http://ao3-browser:8000'}), \
                patch.object(FicbookClient, '_download_once', return_value=expected), \
                patch('src.sources.ao3_browser.download_browser_work') as fallback:
            self.assertIs(FicbookClient().download(expected.source_url), expected)
        fallback.assert_not_called()
