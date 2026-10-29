"""Recognize AO3's maintenance page, not generic failures or story content."""

from enum import Enum
import logging
import os
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class AO3MaintenanceError(Exception):
    pass


class AO3SiteStatus(Enum):
    AVAILABLE = 'available'
    MAINTENANCE = 'maintenance'
    CHALLENGED = 'challenged'
    UNKNOWN = 'unknown'


def is_maintenance(html: bytes | str) -> bool:
    if isinstance(html, bytes):
        html = html.decode('utf-8', 'replace')
    if 'maintenance' not in html.lower():
        return False
    soup = BeautifulSoup(html, 'lxml')
    # A chapter or a future-maintenance banner is still a usable page.
    if soup.select_one('#workskin, #chapters'):
        return False
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    headings = ' '.join(node.get_text(' ', strip=True) for node in soup.select('h1, h2')).lower()
    main = soup.select_one('#main') or soup.body or soup
    text = ' '.join(main.get_text(' ', strip=True).lower().split())
    return bool(('maintenance' in title or 'maintenance' in headings)
                and ('archive of our own' in title or 'ao3' in text)
                and re.search(r'(?:ao3|the archive|archive of our own) (?:is )?(?:temporarily |currently )?'
                              r'down for (?:scheduled )?maintenance', text))


def require_no_maintenance(html: bytes | str) -> None:
    if is_maintenance(html):
        raise AO3MaintenanceError('AO3 сейчас на техническом обслуживании.')


def _probe_http_status() -> AO3SiteStatus:
    # No account, browser, credentials or nested per-page recovery for this one probe.
    from src.sources.ao3_http import AO3Adapter, AO3Session
    try:
        with AO3Session() as session:
            session.trust_env = False
            session.mount('https://', AO3Adapter(max_retries=0))
            response = session.get('https://archiveofourown.org/', timeout=(8, 20))
            try:
                if is_maintenance(response.content):
                    return AO3SiteStatus.MAINTENANCE
                soup = BeautifulSoup(response.content, 'lxml')
                title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
                if (response.headers.get('cf-mitigated') == 'challenge'
                        or any(value in title for value in ('just a moment', 'shields are up', 'not a robot'))):
                    return AO3SiteStatus.CHALLENGED
                if (response.status_code == 200 and title.startswith('home | archive of our own')
                        and soup.select_one('#main')):
                    return AO3SiteStatus.AVAILABLE
                return AO3SiteStatus.UNKNOWN
            finally:
                response.close()
    except Exception as error:
        logger.warning('ao3_status_probe_failed type=%s', type(error).__name__)
        return AO3SiteStatus.UNKNOWN


def probe_status() -> AO3SiteStatus:
    result = _probe_http_status()
    if result in {AO3SiteStatus.UNKNOWN, AO3SiteStatus.CHALLENGED} and os.getenv('AO3_BROWSER_URL'):
        from src.sources.ao3_browser import probe_browser_status
        browser_result = probe_browser_status()
        if browser_result is not AO3SiteStatus.UNKNOWN:
            return browser_result
    return result
