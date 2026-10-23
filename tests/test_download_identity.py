import asyncio
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bs4 import BeautifulSoup

from src.bot import app
from src.bot.ui import download_identity
from src.core.download_queue import DownloadQueuePool
from src.core.models import ChapterPreview, Story, StoryPreview
from src.core.progress import ProgressReporter, report_title
from src.bot.types import PendingChapterSelection
from src.bot.sessions import ExpiringDict
from src.bot.telegram_delivery import TelegramDeliveryGate
from src.sources.collections import FicbookCollection
from src.sources.ficbook import FicbookAccount, FicbookClient
from src.sources.litnet import LitnetClient
from tests import test_ao3_download
from tests.test_download_status_flow import IncomingMessage, NoopAction
from tests.test_ficbook_metadata import FakeStory
from tests.test_fikbooks import FakeClient as FakeFikbooksClient, ROOT
from tests.test_ranobelib import FakeRanobelibClient


class DownloadIdentityTests(unittest.TestCase):
    def test_title_is_escaped_shortened_and_single_line(self):
        result = download_identity(title='<Book & title>\n' + 'x' * 10000)
        self.assertIn('&lt;Book &amp; title&gt;', result)
        self.assertNotIn('\n', result)
        self.assertLess(len(result), 500)
        self.assertIn('...', result)

    def test_url_is_shown_until_title_is_known(self):
        url = 'https://ficbook.net/readfic/123'
        state = app.DownloadProgressState(source_url=url)
        self.assertIn(url, state.status(('txt',)))
        state.set_title('A book')
        self.assertIn('A book', state.status(('txt',)))
        self.assertNotIn(url, state.status(('txt',)))
        state.start_attempt()
        self.assertIn('A book', state.status(('txt',)))

    def test_ordinary_progress_callback_is_unchanged(self):
        events = []
        report_title(events.append, 'Book')
        self.assertEqual(events, [])
        reporter = ProgressReporter(events.append, lambda title: events.append(('title', title)))
        report_title(reporter, 'Book')
        reporter('Скачиваю главы: 1/2')
        self.assertEqual(events, [('title', 'Book'), 'Скачиваю главы: 1/2'])


class SourceTitleProgressTests(unittest.TestCase):
    def reporter(self):
        self.events = []
        return ProgressReporter(lambda text: self.events.append(('progress', text)),
                                lambda title: self.events.append(('title', title)))

    def test_fikbooks_title_precedes_first_chapter_request(self):
        client = FakeFikbooksClient()
        reporter = self.reporter()
        original = client._soup

        def soup(url):
            if url != ROOT:
                self.assertIn(('title', 'Book & title'), self.events)
            return original(url)

        client._soup = soup
        story = client.download(ROOT, reporter)
        self.assertEqual(self.events[0], ('title', story.title))
        self.assertEqual(len(client.requested), 3)

    def test_ranobelib_title_precedes_chapter_progress(self):
        story = FakeRanobelibClient().download('https://ranobelib.me/ru/42--test-novel', self.reporter(), {2})
        self.assertEqual(self.events[0], ('title', story.title))
        self.assertTrue(any(event[0] == 'progress' for event in self.events))

    def test_litnet_title_comes_from_already_fetched_reader(self):
        client = LitnetClient()
        soup = BeautifulSoup('<meta property="og:title" content="Book">'
                             '<select><option value="11">First</option></select>'
                             '<div class="reader-text"><p>Body</p></div>', 'lxml')
        client._soup = Mock(return_value=soup)
        story = client.download('https://litnet.com/ru/reader/book-b1', self.reporter())
        self.assertEqual(self.events[0], ('title', story.title))
        self.assertEqual(client._soup.call_count, 3)

    def test_ao3_title_precedes_whole_work_download(self):
        client, adapter = test_ao3_download.AO3DownloadTests().setup_client()
        story = client._download_fanficfare_once('https://archiveofourown.org/works/1', FicbookAccount(), self.reporter(), None)
        self.assertEqual(self.events[0], ('title', story.title))
        self.assertEqual(adapter.get_request.call_count, 2)

    def test_wattpad_title_precedes_chapter_download(self):
        client, adapter = test_ao3_download.AO3DownloadTests().setup_client()
        story = client._download_fanficfare_once('https://www.wattpad.com/story/123-book', FicbookAccount(), self.reporter(), None)
        self.assertEqual(self.events[0], ('title', story.title))
        adapter.get_request.assert_not_called()

    def test_ficbook_title_does_not_add_metadata_requests(self):
        client = FicbookClient()
        adapter = Mock()
        adapter.get_chapters.return_value = [{'title': 'Chapter', 'url': 'https://ficbook.net/readfic/1/2'}]
        adapter.get_request.return_value = '<html/>'
        adapter.make_soup.return_value = BeautifulSoup('<html/>', 'lxml')
        client._get_story_metadata = Mock(return_value=FakeStory({'title': 'Book', 'author': 'Author'}))
        client._fill_optional_ficbook_metadata = Mock()
        client._build_annotation_html = Mock(return_value='')
        client._extract_cover_image = Mock(return_value=None)
        reporter = self.reporter()

        def chapter(*args):
            self.assertIn(('title', 'Book'), self.events)
            return '<p>Body</p>'

        client._load_chapter_html = Mock(side_effect=chapter)
        with patch('src.sources.ficbook.adapters.getAdapter', return_value=adapter):
            client._download_once('https://ficbook.net/readfic/1', FicbookAccount(), reporter, None)
        client._get_story_metadata.assert_called_once()
        adapter.get_request.assert_called_once()

    def test_hogwartsnet_title_precedes_chapter_request(self):
        client = FicbookClient()
        client._hogwartsnet_opener = Mock(return_value=object())
        client._hogwartsnet_is_locked = Mock(return_value=False)
        client._hogwartsnet_chapter_previews = Mock(return_value=[ChapterPreview(1, 'First')])
        client._hogwartsnet_metadata = Mock(return_value=dict(
            title='Book', author='Author', description='', annotation_html='', start_date='',
            finish_date='', status='', fandoms=[], genres=[], rating='',
        ))
        client._hogwartsnet_soup = Mock(return_value=BeautifulSoup('<p>Body</p>', 'lxml'))
        client._hogwartsnet_chapter = Mock(return_value=('First', '<p>Body</p>'))
        story = client._download_hogwartsnet_once('https://hogwartsnet.ru/mfanf/ffshowfic.php?fid=1',
                                               FicbookAccount(), self.reporter(), None)
        self.assertEqual(self.events[0], ('title', story.title))
        self.assertEqual(client._hogwartsnet_soup.call_count, 2)


class IdentityFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (
            ('_start_download', AsyncMock(return_value=None)), ('_finish_download', AsyncMock()),
            ('_build_selected_files', AsyncMock(return_value=[('txt', b'file')])),
            ('_telegram_edit_gate', app.TelegramEditGate(min_interval=0)),
        ):
            mocked = patch.object(app, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction())
        mocked.start()
        self.addCleanup(mocked.stop)
        mocked = patch('src.bot.telegram_delivery.telegram_delivery',
                       TelegramDeliveryGate(chat_interval=0, bot_interval=0))
        mocked.start()
        self.addCleanup(mocked.stop)

    async def test_preview_title_is_reused_when_entering_queue(self):
        message = IncomingMessage()
        message.text = '1'
        preview = StoryPreview('Known book', 'Author', 'https://ficbook.net/readfic/1', [ChapterPreview(1, 'Chapter')])
        pending = ExpiringDict()
        pending[message.from_user.id] = PendingChapterSelection(preview.source_url, ('txt',), True, preview)
        with patch.object(app, '_chapter_waiting', pending), \
                patch.object(app, '_process_download', AsyncMock()) as download:
            await app.on_chapter_selection_text(message, object(), None, None, object(), DownloadQueuePool(), frozenset())
        self.assertEqual(download.call_args.kwargs['known_title'], 'Known book')

    async def test_known_title_is_shown_in_queue_without_metadata_request(self):
        message = IncomingMessage()
        client = SimpleNamespace(download=Mock(return_value=Story('Known book', 'Author', 'https://ficbook.net/readfic/1')))
        await app._process_download(message, client, None, None, object(), DownloadQueuePool(),
            'https://ficbook.net/readfic/1', ('txt',), frozenset(), known_title='Known book')
        self.assertIn('Known book', message.statuses[0].text)
        client.download.assert_called_once()

    async def test_short_status_does_not_shorten_book_title_or_metadata(self):
        message = IncomingMessage()
        title = 'A very long book title ' * 100
        client = SimpleNamespace(download=Mock(return_value=Story(title, 'Author', 'https://ficbook.net/readfic/1')))
        await app._process_download(message, client, None, None, object(), DownloadQueuePool(),
            'https://ficbook.net/readfic/1', ('txt',), frozenset())
        self.assertLess(len(message.statuses[0].edits[-1]), 600)
        self.assertEqual(app._build_selected_files.call_args.args[0].title, title)
        self.assertEqual(app._finish_download.call_args.kwargs['title'], title)

    async def test_site_recovery_status_keeps_known_title(self):
        state = app.DownloadProgressState(source_url='https://ficbook.net/readfic/1')
        state.set_title('Known book')
        progress = SimpleNamespace(set=AsyncMock())
        client = SimpleNamespace(ficbook_site_status=Mock(return_value=app.FicbookSiteStatus.AVAILABLE))
        with patch.object(app, 'FICBOOK_SITE_RECHECK_SECONDS', 0):
            await app._wait_for_ficbook_recovery(client, DownloadQueuePool(), progress, ('txt',), state=state)
        self.assertTrue(all('Known book' in call.args[0] for call in progress.set.call_args_list))

    async def test_title_visible_before_download_finishes_and_until_sending(self):
        message = IncomingMessage()
        entered, release = Event(), Event()
        title = 'Book <&>'

        def download(url, progress, **kwargs):
            report_title(progress, title)
            progress('Скачиваю главы: 1/2')
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Worker not released')
            return Story(title, 'Author', url)

        task = asyncio.create_task(app._process_download(message, SimpleNamespace(download=download), None, None,
            object(), DownloadQueuePool(), 'https://ficbook.net/readfic/1', ('txt',), frozenset()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await asyncio.sleep(0.03)
            self.assertTrue(any('Book &lt;&amp;&gt;' in text for status in message.statuses for text in status.edits))
            self.assertIn('readfic/1', message.statuses[0].text)
        finally:
            release.set()
            await task
        final = message.statuses[0].edits[-1]
        self.assertIn('Отправляем...', final)
        self.assertIn('Book &lt;&amp;&gt;', final)
        self.assertEqual(message.documents[0]['caption'], 'Book &lt;&amp;&gt; (TXT)')

    async def test_parallel_and_collection_works_have_their_own_titles(self):
        def download(url, progress, **kwargs):
            title = 'Book ' + url.rsplit('/', 1)[-1]
            report_title(progress, title)
            progress('Скачиваю главы: 1/1')
            return Story(title, 'Author', url)

        client = SimpleNamespace(download=download, collection=Mock(return_value=FicbookCollection(
            'Collection', 'https://ficbook.net/collections/1', ('https://ficbook.net/readfic/11', 'https://ficbook.net/readfic/12'))))
        message = IncomingMessage()
        other = IncomingMessage()
        other.from_user.id = 2
        queues = DownloadQueuePool()
        await asyncio.gather(
            app._process_collection(message, client, None, None, object(), queues,
                'https://ficbook.net/collections/1', ('txt',), frozenset()),
            app._process_download(other, client, None, None, object(), queues,
                'https://ficbook.net/readfic/13', ('txt',), frozenset()),
        )
        finals = [text for status in message.statuses for text in status.edits if 'Отправляем...' in text]
        self.assertEqual(len(finals), 2)
        self.assertIn('Book 11', finals[0])
        self.assertIn('Book 12', finals[1])
        self.assertNotIn('Book 13', ''.join(finals))
        self.assertIn('Book 13', other.statuses[0].edits[-1])

