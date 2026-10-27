"""Single-line retry diagnostics without credentials or exception payloads."""

import json
import logging
from pathlib import Path
import re
import traceback
from urllib.parse import urlsplit, urlunsplit

_TOKEN = re.compile(r'[0-9]{6,}:[A-Za-z0-9_-]{20,}')
_URL = re.compile(r'https?://[^\s<>"\']+')
_SECRET = re.compile(r'(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|'
                     r'api[_-]?key|access[_-]?token|refresh[_-]?token|bot[_-]?token|secret)\b\s*[:=][^\r\n]*')


def public_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.scheme not in {'http', 'https'} or not parts.hostname:
            return '[invalid URL]'
        return _TOKEN.sub('[REDACTED_TOKEN]', urlunsplit((parts.scheme, parts.hostname, parts.path, '', '')))[:2048]
    except ValueError:
        return '[invalid URL]'


def safe_reason(value: str) -> str:
    value = _URL.sub(lambda match: public_url(match[0]), value)
    value = _TOKEN.sub('[REDACTED_TOKEN]', value)
    return _SECRET.sub('[REDACTED_SECRET]', value)[:700]


def log_retry(logger, event, *, retry_id, alert_id, url='', reason='', error=None, **fields):
    data = dict(event=event, entry_point='admin_alert_retry', retry_id=retry_id, alert_id=alert_id)
    if url:
        data['url'] = public_url(url)
    if reason:
        data['reason'] = safe_reason(reason)
    data.update(fields)
    if error is not None:
        # Frames and exception classes locate the fault without logging response bodies,
        # headers or the exception's potentially secret message/source-code line.
        chain = []
        seen = set()
        while error is not None and id(error) not in seen and len(chain) < 5:
            seen.add(id(error))
            item = {'type': type(error).__name__, 'stack': [
                f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}'
                for frame in traceback.extract_tb(error.__traceback__)[-8:]]}
            code = getattr(error, 'status_code', getattr(error, 'code', None))
            if isinstance(code, int):
                item['http_status'] = code
            chain.append(item)
            error = error.__cause__
        data['errors'] = chain
    level = logging.WARNING if event.endswith(('failed', 'rejected')) else logging.INFO
    logger.log(level, json.dumps(data, ensure_ascii=False))
