import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fanficfare.exceptions import HTTPErrorFFF
from fanficfare.fetchers.base_fetcher import FetcherResponse

MAINTENANCE = b'<html><title>Down for Maintenance | Archive of Our Own</title><div id="main"><h2>Error 503</h2><p>AO3 is temporarily down for maintenance.</p></div></html>'
URL = 'https://archiveofourown.org/works/1'


class MaintenanceRecognitionTests(unittest.TestCase):
    def test_browser_can_confirm_recovery_when_plain_http_is_challenged(self):
        from src.sources.ao3_status import AO3SiteStatus, probe_status
        with patch.dict('os.environ', {'AO3_BROWSER_URL': 'http://ao3-browser:8000'}), \
                patch('src.sources.ao3_status._probe_http_status', return_value=AO3SiteStatus.CHALLENGED), \
                patch('src.sources.ao3_browser.probe_browser_status', return_value=AO3SiteStatus.AVAILABLE) as browser:
            self.assertIs(probe_status(), AO3SiteStatus.AVAILABLE)
        browser.assert_called_once()

    def test_browser_probe_uses_status_endpoint_and_validates_its_result(self):
        import io, json
        from unittest.mock import Mock
        from src.sources.ao3_status import AO3SiteStatus
        from src.sources.ao3_browser import probe_browser_status
        for body, expected in (({'work_id':'status','status':'available'}, AO3SiteStatus.AVAILABLE),
                               ({'work_id':'status','maintenance':True}, AO3SiteStatus.MAINTENANCE),
                               ({'work_id':'1','status':'available'}, AO3SiteStatus.UNKNOWN)):
            with patch.dict('os.environ', {'AO3_BROWSER_URL': 'http://ao3-browser:8000'}), \
                    patch('src.sources.ao3_browser.build_opener', return_value=Mock(
                        open=Mock(return_value=io.BytesIO(json.dumps(body).encode())))) as opener:
                self.assertIs(probe_browser_status(), expected)
                self.assertEqual(opener.return_value.open.call_args.args[0], 'http://ao3-browser:8000/status')

    def test_download_and_preview_expose_maintenance_as_waitable_domain_error(self):
        from src.sources.ficbook import FicbookClient, AO3SiteUnavailableError
        for method in ('download', 'preview'):
            with self.subTest(method=method), patch('fanficfare.fetchers.RequestsFetcher.request',
                    side_effect=HTTPErrorFFF(URL, 503, 'Unavailable', MAINTENANCE)), \
                    self.assertRaises(AO3SiteUnavailableError):
                getattr(FicbookClient(), method)(URL)

    def test_probe_does_not_claim_recovery_from_generic_errors_or_cloudflare(self):
        from unittest.mock import Mock
        from src.sources.ao3_status import AO3SiteStatus, probe_status
        cases = [(503, MAINTENANCE, {}, AO3SiteStatus.MAINTENANCE),
                 (503, b'<title>Service Unavailable</title>', {}, AO3SiteStatus.UNKNOWN),
                 (403, b'<title>Just a moment</title>', {'cf-mitigated':'challenge'}, AO3SiteStatus.CHALLENGED),
                 (200, b'<title>Home | Archive of Our Own</title><div id="main">Welcome</div>', {}, AO3SiteStatus.AVAILABLE)]
        for code, body, headers, expected in cases:
            response = Mock(status_code=code, content=body, headers=headers)
            with self.subTest(code=code), patch('src.sources.ao3_http.AO3Session') as session:
                session.return_value.__enter__.return_value.get.return_value = response
                self.assertIs(probe_status(), expected)

    def test_recognizes_maintenance_but_not_story_text_challenge_or_generic_503(self):
        from src.sources.ao3_status import is_maintenance
        self.assertTrue(is_maintenance(MAINTENANCE))
        for html in (b'<h1>Service Unavailable</h1>', b'<title>Just a moment</title><p>Cloudflare</p>',
                     b'<div id="workskin"><h2>Down for Maintenance</h2><p>AO3 is temporarily down for maintenance.</p></div>',
                     b'<title>Home | Archive of Our Own</title><div id="main"><p>Scheduled maintenance next week.</p></div>'):
            self.assertFalse(is_maintenance(html))

    def test_fetcher_reports_maintenance_without_retrying_or_using_browser(self):
        from src.sources.ao3_status import AO3MaintenanceError
        from src.sources.ficbook import FicbookClient, FicbookAccount
        fetcher = FicbookClient()._build_config(URL, FicbookAccount()).get_fetcher()
        for answer in (FetcherResponse(MAINTENANCE, URL), HTTPErrorFFF(URL, 503, 'Unavailable', MAINTENANCE)):
            with patch('fanficfare.fetchers.RequestsFetcher.request', side_effect=answer if isinstance(answer,Exception) else None,
                       return_value=answer) as send, patch('src.sources.ao3_http.BrowserCollectionClient') as browser, \
                    self.assertRaises(AO3MaintenanceError):
                fetcher.request('GET', URL)
            self.assertEqual(send.call_count, 1)
            browser.assert_not_called()


class RecoveryMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_monitor_does_not_create_a_task(self):
        from src.core.site_recovery import SiteRecovery
        monitor = SiteRecovery()
        await asyncio.sleep(0)
        self.assertIsNone(monitor.task)
    async def test_one_probe_per_interval_for_multiple_waiters(self):
        from src.core.site_recovery import SiteRecovery
        pause = AsyncMock()
        probe = AsyncMock(side_effect=[False, True])
        monitor = SiteRecovery(interval=300, sleep=pause)
        await asyncio.gather(monitor.wait(probe), monitor.wait(probe))
        self.assertEqual(probe.await_count, 2)
        self.assertEqual([call.args[0] for call in pause.await_args_list], [300, 300])
        self.assertIsNone(monitor.task)

    async def test_last_cancelled_waiter_stops_probes(self):
        from src.core.site_recovery import SiteRecovery
        started = asyncio.Event()
        async def sleep(_):
            started.set()
            await asyncio.Event().wait()
        probe = AsyncMock(return_value=True)
        monitor = SiteRecovery(interval=300, sleep=sleep)
        tasks = [asyncio.create_task(monitor.wait(probe)) for _ in range(2)]
        await started.wait()
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        self.assertFalse(monitor.task.done())
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        self.assertIsNone(monitor.task)
        probe.assert_not_awaited()

    async def test_maintenance_wait_does_not_use_download_deadline(self):
        from src.core.limits import ResourceLimits, resource_scope, suspend_deadline, check_deadline
        clock = [0.0]
        with patch('src.core.limits.monotonic', side_effect=lambda: clock[0]):
            with resource_scope(ResourceLimits(max_job_seconds=1)):
                with suspend_deadline():
                    clock[0] = 7200
                    check_deadline()
                check_deadline()


class MaintenanceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_status_endpoint_does_not_download_a_work(self):
        from unittest.mock import Mock
        from browser.server import Handler
        handler = object.__new__(Handler)
        handler.path = '/status'
        handler.respond = Mock()
        with patch('browser.server.download', return_value=b'{"work_id":"status","status":"available"}') as download:
            handler.do_GET()
        download.assert_called_once_with('status')
        self.assertEqual(handler.respond.call_args.args[0], 200)
    async def test_empty_queue_cancellation_clears_stale_maintenance_flag(self):
        from types import SimpleNamespace
        from src.bot import app
        from src.core.download_queue import DownloadQueuePool
        queues = DownloadQueuePool()
        queues.mark_site_unavailable('archiveofourown.org')
        entered = asyncio.Event()
        async def sleep(_):
            entered.set()
            await asyncio.Event().wait()
        queues.ao3_recovery.sleep = sleep
        task = asyncio.create_task(app._wait_for_ao3_recovery(queues, SimpleNamespace(set=AsyncMock()), ('epub',)))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(queues.is_site_unavailable('archiveofourown.org'))
        self.assertIsNone(queues.ao3_recovery.task)

    async def test_browser_worker_reports_maintenance_separately(self):
        from unittest.mock import Mock
        from browser.server import Handler
        handler = object.__new__(Handler)
        handler.path = '/works/1'
        handler.respond = Mock()
        with patch('browser.server.download', return_value=b'{"work_id":"1","maintenance":true}'):
            handler.do_GET()
        self.assertEqual(handler.respond.call_args.args[0], 409)

    async def test_queue_resumes_and_preserves_selected_formats_and_chapters(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.bot import app
        from src.core.download_queue import DownloadQueuePool
        from src.core.models import Story
        from src.sources.ficbook import AO3SiteUnavailableError
        from src.sources.ao3_status import AO3SiteStatus
        from tests.test_download_status_flow import IncomingMessage, NoopAction
        queues = DownloadQueuePool()
        queues.ao3_recovery.interval = 0.001
        client = SimpleNamespace(download=Mock(side_effect=[AO3SiteUnavailableError('maintenance'),
                                                           Story('Book', 'Writer', URL)]))
        message = IncomingMessage()
        with patch.object(app, '_start_download', AsyncMock()), \
                patch.object(app, '_finish_download', AsyncMock()), \
                patch.object(app, '_notify_admin', AsyncMock()) as notify, \
                patch.object(app, '_build_selected_files', AsyncMock(return_value=[('epub', b'file')])), \
                patch.object(app.ChatActionSender, 'upload_document', return_value=NoopAction()), \
                patch.object(app, 'probe_ao3_status', Mock(side_effect=[AO3SiteStatus.MAINTENANCE, AO3SiteStatus.AVAILABLE])) as probe:
            await app._process_download(message, client, None, None, object(), queues,
                                        URL, ('epub',), frozenset(), chapter_numbers={2})
        self.assertEqual(client.download.call_count, 2)
        self.assertEqual(client.download.call_args.kwargs['chapter_numbers'], {2})
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(message.documents), 1)
        self.assertTrue(any('AO3 сейчас на техническом обслуживании' in text for text in message.statuses[0].edits))
        notify.assert_not_awaited()
        self.assertFalse(queues.is_site_unavailable('archiveofourown.org'))
        self.assertIsNone(queues.ao3_recovery.task)

    async def test_preview_waits_for_recovery_and_does_not_raise_an_alert(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.bot import app
        from src.core.download_queue import DownloadQueuePool
        from src.core.models import ChapterPreview, StoryPreview
        from src.sources.ficbook import AO3SiteUnavailableError
        from src.sources.ao3_status import AO3SiteStatus
        from tests.test_download_status_flow import IncomingMessage
        queues = DownloadQueuePool()
        queues.ao3_recovery.interval = 0.001
        client = SimpleNamespace(preview=Mock(side_effect=[AO3SiteUnavailableError('maintenance'),
            StoryPreview('Book', 'Writer', URL, [ChapterPreview(1, 'First')])]))
        message = IncomingMessage()
        with patch.object(app, '_notify_admin', AsyncMock()) as notify, \
                patch.object(app, '_answer_status_with_retry', AsyncMock(return_value=SimpleNamespace(
                    edit_text=AsyncMock(), delete=AsyncMock(), bot=message.bot, chat=message.chat))), \
                patch.object(app, 'probe_ao3_status', Mock(return_value=AO3SiteStatus.AVAILABLE)) as probe:
            await app._ask_chapter_selection(message, client, None, None, URL, ('epub',), True, queues)
        self.assertEqual(client.preview.call_count, 2)
        self.assertEqual(probe.call_count, 1)
        notify.assert_not_awaited()
        self.assertIn(message.from_user.id, app._chapter_waiting)
        app._chapter_waiting.pop(message.from_user.id, None)

