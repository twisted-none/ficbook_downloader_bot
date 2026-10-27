from copy import deepcopy
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from src.core.download_queue import DownloadQueuePool
from src.core.limits import ResourceLimitError, ResourceLimits, resource_scope
from src.core.progress import ProgressReporter
from src.exporters.navigation import prepare_chapters
from src.sources.registry import is_supported_story_url, normalize_url, site_display_name
from src.sources.share_links import extract_download_url
from src.sources.zahleb import ZahlebClient, ZahlebError, ZahlebAccessError, ZahlebNotFoundError, ZahlebRateLimitError

ROOT = 'https://zahleb.me/story/test-book-lBH5frx0bi'
FIRST, SECOND = 'nEZoFrmTKQ', 'B7hIYeJjKg'
BOOK = dict(objectId='lBH5frx0bi', title='Book <&>', shortAuthor='Author', subtitle='Description',
            episodesCount='2', continued=False, contentType='combo', ageRating=18,
            genres=[{'title': 'Drama'}], marks=[{'title': 'Magic'}], fanbase=[{'title': 'Fandom'}],
            pairs=[{'title': 'One / Two'}], chapters=[
                {'order': 2, 'objectId': SECOND, 'title': 'Second', 'releasedAt': '20.07.2022'},
                {'order': 1, 'objectId': FIRST, 'title': 'First', 'releasedAt': '19.07.2022'}])


def episode(identifier=FIRST, order=1):
    return dict(objectId=identifier, order=order, story={'objectId': BOOK['objectId']}, status='published',
                content=[{'align': 'left', 'children': [{'text': 'Chapter text', 'bold': True}]},
                         {'name': 'Character', 'text': 'A reply'}])


class FakeClient(ZahlebClient):
    def __init__(self):
        super().__init__(request_delay=0)
        self.book = deepcopy(BOOK)
        self.episodes = {FIRST: episode(), SECOND: episode(SECOND, 2)}
        self.requested = []

    def _page(self, url):
        self.requested.append(url)
        result = {'story': deepcopy(self.book)}
        if url != ROOT:
            result['episode'] = deepcopy(self.episodes[url.rsplit('/', 1)[1]])
        return result


class ZahlebTests(unittest.TestCase):
    def test_story_and_chapter_links_are_recognized(self):
        for url in (ROOT, ROOT + '/' + FIRST, ROOT + '/' + FIRST + '#reader',
                    ROOT + '?utm_source=telegram', ROOT.replace('https://', 'http://www.')):
            with self.subTest(url=url):
                self.assertTrue(is_supported_story_url(url))
                self.assertEqual(normalize_url(url), ROOT)
                self.assertEqual(extract_download_url('Book ' + url), url)
        self.assertEqual(site_display_name(ROOT), 'Взахлёб')

    def test_unsafe_urls_are_rejected(self):
        for url in ('https://zahleb.me.evil/story/test-lBH5frx0bi',
                    'https://user@zahleb.me/story/test-lBH5frx0bi', ROOT + '/../../secret',
                    ROOT.replace('zahleb.me', 'zahleb.me:444'), ROOT + '?next=http://127.0.0.1',
                    ROOT + '/bad', ROOT + '/%2e%2e', ROOT + '\\bad', ROOT + '/a/b',
                    'https://zahleb.me/catalog'):
            with self.subTest(url=url):
                self.assertFalse(is_supported_story_url(url))

    def test_preview_has_ordered_chapters_and_does_not_download_text(self):
        client = FakeClient()
        preview = client.preview(ROOT + '/' + SECOND)
        self.assertEqual(preview.title, 'Book <&>')
        self.assertEqual(preview.author, 'Author')
        self.assertEqual([c.title for c in preview.chapters], ['First', 'Second'])
        self.assertEqual(client.requested, [ROOT])

    def test_download_has_metadata_progress_and_whole_story_from_chapter_link(self):
        client, events = FakeClient(), []
        story = client.download(ROOT + '/' + SECOND,
            ProgressReporter(lambda text: events.append(text), lambda title: events.append(('title', title))))
        self.assertEqual(events[0], ('title', 'Book <&>'))
        self.assertTrue(any(isinstance(e, str) and '2/2' in e for e in events))
        self.assertEqual(client.requested, [ROOT, ROOT + '/' + FIRST, ROOT + '/' + SECOND])
        self.assertEqual(story.author, 'Author')
        self.assertEqual(story.status, 'Завершён')
        self.assertEqual(story.rating, '18+')
        self.assertEqual(story.fandoms, ['Fandom'])
        self.assertEqual(story.genres, ['Drama', 'Magic'])
        self.assertEqual(story.pairings, ['One / Two'])
        self.assertIn('<strong>Chapter text</strong>', story.chapters[0].html)
        self.assertIn('Character', story.chapters[0].html)
        self.assertEqual(len(story.chapters), 2)

    def test_selected_chapters_only(self):
        client = FakeClient()
        story = client.download(ROOT, chapter_numbers={2})
        self.assertEqual(client.requested, [ROOT, ROOT + '/' + SECOND])
        self.assertEqual(story.chapters[0].title, 'Second')
        for selected in (set(), {0}, {3}):
            with self.subTest(selected=selected), self.assertRaises(ZahlebError):
                FakeClient().download(ROOT, chapter_numbers=selected)

    def test_missing_duplicate_or_out_of_order_table_of_contents_fails(self):
        for change in ('missing', 'duplicate', 'gap', 'foreign-id'):
            client = FakeClient()
            if change == 'missing':
                client.book['chapters'].pop()
            elif change == 'duplicate':
                client.book['chapters'][0]['objectId'] = FIRST
            elif change == 'gap':
                client.book['chapters'][0]['order'] = 3
            else:
                client.book['chapters'][0]['objectId'] = 'https://127.0.0.1'
            with self.subTest(change=change), self.assertRaises(ZahlebError):
                client.download(ROOT)

    def test_unavailable_or_empty_chapter_never_returns_partial_story(self):
        for change in ('empty', 'missing', 'wrong-book', 'wrong-id', 'wrong-order', 'truncated', 'paid', 'draft'):
            client = FakeClient()
            ep = client.episodes[SECOND]
            if change == 'empty': ep['content'] = []
            if change == 'missing': ep.pop('content')
            if change == 'wrong-book': ep['story']['objectId'] = 'OtherBook1'
            if change == 'wrong-id': ep['objectId'] = FIRST
            if change == 'wrong-order': ep['order'] = 1
            if change == 'truncated': ep['contentLength'] = 100000
            if change == 'paid': ep['isFree'] = False
            if change == 'draft': ep['status'] = 'draft'
            with self.subTest(change=change), self.assertRaises(ZahlebError):
                client.download(ROOT)

    def test_rich_text_is_escaped_and_anchors_remain_internal(self):
        client = FakeClient()
        client.episodes[FIRST]['content'] = [
            {'children': [{'text': '<script>evil()</script>', 'italic': True},
                          {'type': 'link', 'url': '#note', 'children': [{'text': '[1]'}]}]},
            {'id': 'note', 'children': [{'text': 'Footnote', 'superscript': True}]},
            {'children': [{'type': 'link', 'url': 'javascript:evil()', 'children': [{'text': 'Link'}]}]}]
        html = prepare_chapters(client.download(ROOT))[0].html
        self.assertNotIn('<script>', html)
        self.assertNotIn('javascript:', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('href="#c0001-', html)
        self.assertIn('<sup>', html)

    def test_comics_and_video_are_not_silently_exported_as_text(self):
        for flag in ('isComics', 'isVideo'):
            client = FakeClient()
            client.book[flag] = True
            with self.subTest(flag=flag), self.assertRaises(ZahlebError):
                client.download(ROOT)

    def test_zahleb_uses_its_own_site_queue(self):
        queues = DownloadQueuePool()
        self.assertIs(queues.assign(ROOT).queue, queues.assign(ROOT + '/' + FIRST).queue)
        self.assertIsNot(queues.assign(ROOT).queue, queues.assign('https://ficbook.net/readfic/1').queue)
        self.assertIsNone(queues.assign(ROOT).account)

    def test_recursive_content_and_chapter_budget_are_bounded(self):
        client = FakeClient()
        node = {'text': 'Text'}
        for _ in range(60): node = {'children': [node]}
        client.episodes[FIRST]['content'] = [node]
        with self.assertRaises(ResourceLimitError): client.download(ROOT)
        with resource_scope(ResourceLimits(max_chapters=1)), self.assertRaises(ResourceLimitError):
            FakeClient().download(ROOT)

    def test_public_page_json_is_parsed_without_executing_scripts(self):
        client = ZahlebClient()
        data = json.dumps({'props': {'pageProps': {'story': BOOK}}})
        response = BytesIO(('<script>do_not_execute()</script><script id="__NEXT_DATA__" type="application/json">'
                            + data + '</script>').encode())
        response.geturl = lambda: ROOT
        with patch.object(client._opener, 'open', return_value=response):
            self.assertEqual(client.preview(ROOT).author, 'Author')

    def test_http_errors_have_clear_types(self):
        for code, error in ((404, ZahlebNotFoundError), (429, ZahlebRateLimitError), (403, ZahlebAccessError)):
            client = ZahlebClient()
            with self.subTest(code=code), patch.object(client._opener, 'open',
                    side_effect=HTTPError(ROOT, code, 'Error', {}, BytesIO(b''))), self.assertRaises(error):
                client.preview(ROOT)

    def test_cover_uses_only_known_hosts_and_checks_pixels(self):
        from PIL import Image
        client = ZahlebClient()
        with patch.object(client._cover_opener, 'open') as fetch:
            for url in ('https://127.0.0.1/cover.png', 'https://evil.example/cover.png',
                        'http://files.zahleb.me/cover.png', 'https://files.zahleb.me@evil.example/cover.png'):
                self.assertIsNone(client._cover(url))
            fetch.assert_not_called()
        output = BytesIO()
        Image.new('RGB', (40, 60), 'white').save(output, 'PNG')
        with patch.object(client._cover_opener, 'open', return_value=BytesIO(output.getvalue())):
            self.assertEqual(client._cover('https://files.zahleb.me/cover.png').mime, 'image/png')
        with resource_scope(ResourceLimits(max_cover_pixels=100)), \
                patch.object(client._cover_opener, 'open', return_value=BytesIO(output.getvalue())):
            self.assertIsNone(client._cover('https://files.zahleb.me/cover.png'))

    def test_page_redirect_cannot_change_the_story_or_chapter(self):
        client = ZahlebClient()
        for target in ('https://zahleb.me/story/other-OtherBook1', ROOT + '/' + FIRST,
                       'https://evil.example/book'):
            response = BytesIO(b'not needed')
            response.geturl = lambda: target
            with self.subTest(target=target), patch.object(client._opener, 'open', return_value=response), \
                    self.assertRaises(ZahlebError):
                client.preview(ROOT)

    def test_story_may_redirect_to_first_chapter_of_the_same_story(self):
        client = ZahlebClient()
        state = {'props': {'pageProps': {'story': BOOK, 'episode': episode()}}}
        response = BytesIO(('<script id="__NEXT_DATA__">' + json.dumps(state) + '</script>').encode())
        response.geturl = lambda: ROOT + '/' + FIRST
        with patch.object(client._opener, 'open', return_value=response):
            self.assertEqual(client.preview(ROOT).title, BOOK['title'])

    def test_root_redirect_must_be_first_chapter_and_match_embedded_identity(self):
        for target, data in ((SECOND, {'story': BOOK, 'episode': episode(SECOND, 2)}),
                             (FIRST, {'story': BOOK, 'episode': episode(SECOND, 2)}),
                             (FIRST, {'story': dict(BOOK, objectId='OtherBook1'), 'episode': episode()})):
            response = BytesIO(('<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': data}})
                                + '</script>').encode())
            response.geturl = lambda: ROOT + '/' + target
            client = ZahlebClient()
            with self.subTest(target=target, data=data), patch.object(client._opener, 'open', return_value=response), \
                    self.assertRaises(ZahlebError):
                client.preview(ROOT)

    def test_requested_chapter_may_not_redirect_to_another_chapter(self):
        client = ZahlebClient()
        response = BytesIO(b'not needed')
        response.geturl = lambda: ROOT + '/' + FIRST
        with patch.object(client._opener, 'open', return_value=response), self.assertRaises(ZahlebError):
            client._page(ROOT + '/' + SECOND)

    def test_broken_or_ambiguous_embedded_state_is_not_accepted(self):
        for content in ('<h1>Browser check</h1>', '<script id="__NEXT_DATA__">{invalid}</script>',
                        '<script id="__NEXT_DATA__">{}</script>' * 2,
                        '<script id="__NEXT_DATA__">{"props":{"pageProps":[]}}</script>'):
            client = ZahlebClient()
            response = BytesIO(content.encode())
            response.geturl = lambda: ROOT
            with self.subTest(content=content), patch.object(client._opener, 'open', return_value=response), \
                    self.assertRaises(ZahlebError):
                client.preview(ROOT)

    def test_utf16_content_length_accepts_emoji(self):
        client = FakeClient()
        client.episodes[FIRST]['content'] = [{'text': '\U0001f642'}]
        client.episodes[FIRST]['contentLength'] = 2
        self.assertEqual(len(client.download(ROOT, chapter_numbers={1}).chapters), 1)

    def test_json_encoded_text_length_is_accepted_without_length_tolerance(self):
        client = FakeClient()
        content = [{'children': [{'text': 'Quoted "text" \U0001f642\n'}]}]
        client.episodes[FIRST]['content'] = content
        declared = sum(len(json.dumps(text, ensure_ascii=False).encode('utf-16-le')) // 2
                       for text in ('', content[0]['children'][0]['text']))
        client.episodes[FIRST]['contentLength'] = declared
        self.assertEqual(len(client.download(ROOT, chapter_numbers={1}).chapters), 1)
        client.episodes[FIRST]['contentLength'] = declared - 1
        with self.assertRaises(ZahlebError):
            client.download(ROOT, chapter_numbers={1})

    def test_malformed_rich_text_fails_as_source_error(self):
        for content in ([{'type': [], 'text': 'Text'}], [{'text': 42}], [{'children': {}}],
                        [{'text': '\ud800'}], [None]):
            client = FakeClient()
            client.episodes[FIRST]['content'] = content
            with self.subTest(content=repr(content)), self.assertRaises(ZahlebError):
                client.download(ROOT)
