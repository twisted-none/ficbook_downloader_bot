"""Fresh anonymous browser for one public work. No CAPTCHA interaction."""

import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, '/app')

from camoufox import DefaultAddons
from camoufox.sync_api import Camoufox
from playwright.sync_api import Error, TimeoutError as BrowserTimeout

from browser.transport import HOSTS, allowed_url, native_transport

MAX_BYTES = 16 * 1024 * 1024


def work_document(page):
    return page.evaluate('''() => {
      const root = document.querySelector('#main');
      if (!root || !root.querySelector('#workskin h2.title')) return '';
      const copy = root.cloneNode(true);
      copy.querySelectorAll('script, style, iframe, form, input, button, img, video, audio, svg, link')
          .forEach(node => node.remove());
      return copy.outerHTML;
    }''')


def complete(page):
    return page.evaluate('''() => {
      const text = document.querySelector('dd.chapters')?.textContent.trim() || '';
      const match = text.match(/^([\\d,]+)\\/([\\d,]+|\\?)$/);
      const root = document.querySelector('#chapters');
      if (!root || !match) return false;
      const expected = Number(match[1].replaceAll(',', ''));
      let groups = [...root.querySelectorAll(':scope > .chapter')];
      if (!groups.length && expected === 1) groups = [root];
      return groups.length === expected && groups.every(group => {
        const bodies = group.querySelectorAll(':scope > .userstuff');
        return bodies.length === 1 && bodies[0].textContent.trim();
      });
    }''')


def run(identifier):
    origin = f'https://archiveofourown.org/works/{identifier}'
    with native_transport() as proxy, Camoufox(
        executable_path='/opt/camoufox/camoufox-bin', headless=False, geoip=False,
        block_webrtc=True, exclude_addons=list(DefaultAddons), timeout=60000,
        proxy={'server': proxy}, firefox_user_prefs={
            'network.trr.mode': 5, 'network.dns.disablePrefetch': True,
            'network.prefetch-next': False, 'network.http.http3.enable': False},
    ) as browser:
        context = browser.new_context(service_workers='block', accept_downloads=True)
        count = 0
        def route(handler):
            nonlocal count
            request = handler.request
            count += 1
            if count > 200 or not allowed_url(request.url):
                return handler.abort()
            parts = urlsplit(request.url)
            if request.is_navigation_request() and request.frame == page.main_frame:
                if parts.hostname not in HOSTS - {'challenges.cloudflare.com'} or not (
                    re.fullmatch(rf'/works/{identifier}(?:/navigate)?/?', parts.path)
                    or parts.path.startswith(f'/downloads/{identifier}/') and parts.path.endswith('.html')
                    or parts.path.startswith('/cdn-cgi/')):
                    return handler.abort()
            if request.method not in {'GET', 'HEAD'} and not (
                    parts.hostname == 'challenges.cloudflare.com' or parts.path.startswith('/cdn-cgi/')):
                return handler.abort()
            handler.continue_()
        context.route('**/*', route)
        context.route_web_socket('**/*', lambda websocket: websocket.close())
        page = context.new_page()
        page.set_default_timeout(12000)
        downloads = []
        page.on('download', lambda download: downloads.append(download))

        def navigate(url):
            try:
                response = page.goto(url, wait_until='domcontentloaded', timeout=25000)
                if response and response.status in {401, 404, 429}:
                    raise ValueError(f'AO3 HTTP {response.status}')
                if response and response.status == 403 and not response.headers.get('cf-mitigated') == 'challenge':
                    raise ValueError('AO3 denied public access')
            except Error:
                # A file download interrupts navigation by design; the download event owns the result.
                if not downloads:
                    return False
            return True

        navigate(origin + '?view_full_work=true&view_adult=true')
        try:
            page.locator('#workskin h2.title').wait_for(timeout=12000)
        except BrowserTimeout:
            pass
        document = work_document(page)
        if document and complete(page):
            return {'work_id': identifier, 'page': document, 'download': ''}
        # Open the ordinary work first, preserving this anonymous browser session for Download.
        navigate(origin + '?view_adult=true')
        try:
            page.locator('#workskin h2.title').wait_for(timeout=12000)
        except BrowserTimeout:
            raise ValueError('AO3 did not expose a public work') from None
        document = work_document(page)
        # No work metadata means login/private/mystery/challenge, never a reason to request a file.
        if not document or not page.locator('dd.chapters').count():
            raise ValueError('Missing public work metadata')
        links = page.locator('li.download a[href]').evaluate_all('(links) => links.map(link => link.href)')
        target = next((link for link in links if allowed_url(link)
                       and urlsplit(link).path.startswith(f'/downloads/{identifier}/')
                       and urlsplit(link).path.endswith('.html')), None)
        if target is None:
            # Official routes identify a work by ID; the filename is cosmetic.
            target = f'https://archiveofourown.org/downloads/{identifier}/work.html'
        navigate(origin + '/navigate?view_adult=true')
        navigate(target)
        if not downloads:
            # Some responses are inline HTML, rather than attachments.
            if urlsplit(page.url).path.startswith(f'/downloads/{identifier}/'):
                html = page.content()
                return {'work_id': identifier, 'page': document, 'download': html}
            raise ValueError('AO3 did not provide its HTML download')
        download = downloads[-1]
        parts = urlsplit(download.url)
        if (not allowed_url(download.url) or not parts.path.startswith(f'/downloads/{identifier}/')
                or not parts.path.endswith('.html')):
            download.cancel()
            raise ValueError('Unexpected download destination')
        path = Path(download.path())
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('AO3 file too large')
        return {'work_id': identifier, 'page': document, 'download': path.read_text(encoding='utf-8')}


if __name__ == '__main__':
    identifier = sys.argv[1] if len(sys.argv) == 2 else ''
    if not re.fullmatch(r'[1-9][0-9]{0,15}', identifier):
        raise SystemExit(2)
    result = json.dumps(run(identifier), ensure_ascii=False).encode('utf-8')
    if len(result) > MAX_BYTES:
        raise SystemExit(3)
    sys.stdout.buffer.write(result)
