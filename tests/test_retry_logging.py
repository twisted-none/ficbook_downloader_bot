import json
import logging
import unittest

from src.core.retry_logging import log_retry, public_url, safe_reason


class RetryLoggingTests(unittest.TestCase):
    def test_credentials_queries_and_tokens_are_not_logged(self):
        token = '123456789:abcdefghijklmnopqrstuvwxyz123456789'
        reason = f'Error https://name:password@example.test/work?access_token=secret#private\nCookie: private=value\n{token}'
        result = safe_reason(reason)
        for secret in ('name:', 'password@', '?access_token', '#private', 'private=value', token):
            self.assertNotIn(secret, result)
        self.assertIn('https://example.test/work', result)
        self.assertEqual(public_url('https://user:pass@site.test/path?q=secret#fragment'), 'https://site.test/path')

    def test_exception_stack_is_useful_without_printing_exception_body(self):
        try:
            raise ValueError('private response body and credentials')
        except ValueError as error:
            with self.assertLogs('retry_test', level='WARNING') as output:
                log_retry(logging.getLogger('retry_test'), 'alert_retry_failed', retry_id='abc',
                          alert_id=12, error=error, reason='Source unavailable')
        data = json.loads(output.records[0].message)
        self.assertEqual(data['errors'][0]['type'], 'ValueError')
        self.assertIn('test_retry_logging.py', data['errors'][0]['stack'][0])
        self.assertNotIn('private response body', output.records[0].message)
