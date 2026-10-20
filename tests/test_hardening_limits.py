from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from src.core.limits import (LimitedBytesIO, ResourceLimitError, ResourceLimits,
                             image_is_safe, read_bounded, resource_scope)
from src.core.models import Chapter
from src.bot.sessions import ExpiringDict, ExpiringSet
from src.storage.users import UserStore, UserStoreError


class ResourceLimitTests(unittest.TestCase):
    def test_truncated_content_length_is_not_accepted_as_a_complete_book(self):
        response = BytesIO(b'part')
        response.headers = {'Content-Length': '20'}
        with self.assertRaises(URLError):
            read_bounded(response)

    def test_cover_dimensions_are_checked_without_decoding_pixels(self):
        from tests.test_formats_cover import PNG_1X1
        self.assertTrue(image_is_safe(PNG_1X1))
        with resource_scope(replace(ResourceLimits(), max_cover_pixels=1)):
            with patch('PIL.Image.open') as image:
                image.return_value.__enter__.return_value.width = 10000
                image.return_value.__enter__.return_value.height = 10000
                self.assertFalse(image_is_safe(PNG_1X1))
        self.assertFalse(image_is_safe(b'invalid image'))

    def test_output_stream_refuses_growth_before_allocating_it(self):
        with resource_scope(replace(ResourceLimits(), max_file_bytes=5)):
            stream = LimitedBytesIO()
            stream.write(b'12345')
            stream.seek(0)
            stream.write(b'12')
            stream.seek(0, 2)
            with self.assertRaises(ResourceLimitError):
                stream.write(b'6')
            self.assertEqual(stream.getvalue(), b'12345')

    def test_response_boundary_and_oversized_body(self):
        self.assertEqual(read_bounded(BytesIO(b'12345'), 5), b'12345')
        with self.assertRaises(ResourceLimitError):
            read_bounded(BytesIO(b'123456'), 5)

    def test_chapters_are_counted_before_a_whole_book_accumulates(self):
        with resource_scope(replace(ResourceLimits(), max_chapters=2)):
            Chapter.from_download('one', '<p>body</p>')
            Chapter.from_download('two', '<p>body</p>')
            with self.assertRaises(ResourceLimitError):
                Chapter.from_download('three', '<p>body</p>')

    def test_total_html_bytes_are_bounded_and_scope_is_reset(self):
        with resource_scope(replace(ResourceLimits(), max_story_bytes=5)):
            Chapter.from_download('one', '123')
            with self.assertRaises(ResourceLimitError):
                Chapter.from_download('two', '456')
        Chapter.from_download('outside', '123456')

    def test_expired_budget_does_not_read_from_response(self):
        body = BytesIO(b'body')
        with patch('src.core.limits.monotonic', return_value=0) as clock:
            with self.assertRaises(ResourceLimitError), resource_scope(replace(ResourceLimits(), max_job_seconds=1)):
                clock.return_value = 2
                read_bounded(body)
            self.assertEqual(body.tell(), 0)


class SessionExpiryTests(unittest.TestCase):
    def test_dictionary_expires_and_updates_idle_deadline_on_access(self):
        with patch('src.bot.sessions.monotonic', return_value=0):
            sessions = ExpiringDict(ttl_seconds=30)
            sessions[1] = 'draft'
        with patch('src.bot.sessions.monotonic', return_value=20):
            self.assertEqual(sessions.get(1), 'draft')
        with patch('src.bot.sessions.monotonic', return_value=49):
            self.assertEqual(sessions.get(1), 'draft')
        with patch('src.bot.sessions.monotonic', return_value=80):
            self.assertNotIn(1, sessions)
            self.assertEqual(len(sessions), 0)

    def test_waiting_sets_expire_and_size_is_bounded(self):
        with patch('src.bot.sessions.monotonic', return_value=0):
            sessions = ExpiringSet(ttl_seconds=30, max_entries=2)
            sessions.add(1)
            sessions.add(2)
            sessions.add(3)
            self.assertNotIn(1, sessions)
        with patch('src.bot.sessions.monotonic', return_value=31):
            self.assertEqual(len(sessions), 0)


class UserStoreSafetyTests(unittest.TestCase):
    def test_corrupt_file_is_not_overwritten_as_an_empty_list(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'users.json'
            path.write_text('{broken', encoding='utf-8')
            with self.assertRaises(UserStoreError):
                UserStore(path).add(2)
            self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_interrupted_replace_preserves_previous_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'users.json'
            store = UserStore(path)
            store.add(1)
            with patch('src.storage.users.os.replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    store.add(2)
            self.assertEqual(UserStore(path).list_user_ids(), [1])
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_json_shape_and_values_are_not_silently_dropped(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'users.json'
            for content in ('{}', '[1, true]', '[1, -2]', '[1, "bad"]'):
                path.write_text(content, encoding='utf-8')
                with self.subTest(content=content), self.assertRaises(UserStoreError):
                    UserStore(path).list_user_ids()


if __name__ == '__main__':
    unittest.main()
