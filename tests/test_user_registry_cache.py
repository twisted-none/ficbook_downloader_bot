from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.storage.users import UserStore, UserStoreError


class UserRegistryCacheTests(unittest.TestCase):
    def test_known_user_does_not_reread_whole_json(self):
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / 'users.json')
            store.add(1)
            with patch.object(Path, 'read_text', side_effect=AssertionError('Unexpected reread')):
                store.add(1)
                store.add(1)
                self.assertEqual(store.list_user_ids(), [1])

    def test_external_corruption_is_not_hidden_by_cache(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'users.json'
            store = UserStore(path)
            store.add(1)
            path.write_text('{broken externally}', encoding='utf-8')
            with self.assertRaises(UserStoreError):
                store.add(2)
            self.assertEqual(path.read_text(encoding='utf-8'), '{broken externally}')
