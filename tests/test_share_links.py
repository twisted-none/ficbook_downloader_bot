import socket
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.sources.share_links import (
    PublicResolver, ShareLinkError, extract_download_url, is_google_share_url,
    resolve_share_url, _fetch_redirect, MAX_RESPONSE_BYTES,
)


def public_address(ip="8.8.8.8"):
    return dict(hostname="share.google", host=ip, port=443, family=socket.AF_INET, proto=0, flags=0)


class ShareLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_only_exact_https_google_share_links_are_accepted(self):
        self.assertTrue(is_google_share_url("https://share.google/Ab123"))
        for url in ["http://share.google/Ab123", "https://share.google.evil.test/Ab123",
                    "https://evil.test/share.google/Ab123", "https://user@share.google/Ab123",
                    "https://share.google:8443/Ab123", "https://share.google/", "https://share.google/a/b",
                    "https://share.google/Ab123?url=http://localhost", "https://share.google/Ab123\\evil"]:
            with self.subTest(url=url):
                self.assertFalse(is_google_share_url(url))

    def test_extraction_keeps_first_supported_candidate(self):
        self.assertEqual(extract_download_url("Жалоба: https://share.google/Ab123 https://ficbook.net/readfic/1"), "https://share.google/Ab123")
        self.assertEqual(extract_download_url("https://example.org https://ficbook.net/readfic/1"), "https://ficbook.net/readfic/1")

    async def test_real_google_redirect_shape_resolves_to_supported_story(self):
        with patch("src.sources.share_links._fetch_redirect", new_callable=AsyncMock,
                   side_effect=["https://www.google.com/share.google?q=Ab123", "https://ficbook.net/readfic/123"]) as fetch, patch(
            "src.sources.share_links.ThreadedResolver.resolve", new_callable=AsyncMock, return_value=[public_address()]
        ):
            self.assertEqual(await resolve_share_url("https://share.google/Ab123"), "https://ficbook.net/readfic/123")
            self.assertEqual(fetch.await_count, 2)

    async def test_google_share_can_resolve_to_a_public_collection_url(self):
        target = 'https://ficbook.net/collections/123'
        with patch('src.sources.share_links._fetch_redirect', AsyncMock(return_value=target)), patch(
            'src.sources.share_links.ThreadedResolver.resolve', AsyncMock(return_value=[public_address()]),
        ):
            self.assertEqual(await resolve_share_url('https://share.google/Ab123'), target)

    async def test_redirect_targets_are_validated_before_fetching(self):
        targets = ["http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data",
                   "https://[::1]/", "file:///etc/passwd", "https://evil.test/",
                   "https://www.google.com/url?q=https://evil.test", "https://ficbook.net.evil.test/readfic/1",
                   "https://x@ficbook.net/readfic/1", "https://ficbook.net:8080/readfic/1",
                   "https://ficbook.net/authors/1", "https://share.google.evil.test/Ab123"]
        for target in targets:
            with self.subTest(target=target), patch("src.sources.share_links._fetch_redirect", new_callable=AsyncMock, return_value=target) as fetch:
                with self.assertRaises(ShareLinkError):
                    await resolve_share_url("https://share.google/Ab123")
                fetch.assert_awaited_once()

    async def test_cycles_and_hop_limit_are_rejected(self):
        for targets in [["https://share.google/Ab123"], [f"https://share.google/A{i}" for i in range(10)]]:
            with patch("src.sources.share_links._fetch_redirect", new_callable=AsyncMock, side_effect=targets):
                with self.assertRaises(ShareLinkError):
                    await resolve_share_url("https://share.google/Ab123")

    async def test_resolver_rejects_any_nonpublic_dns_answer(self):
        for ip in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "224.0.0.1",
                   "::1", "fc00::1", "fec0::1", "::ffff:127.0.0.1", "2001:db8::1"]:
            with self.subTest(ip=ip), patch("src.sources.share_links.ThreadedResolver.resolve", new_callable=AsyncMock,
                                           return_value=[public_address(), public_address(ip)]):
                resolver = PublicResolver()
                try:
                    with self.assertRaises(ShareLinkError):
                        await resolver.resolve("share.google", 443)
                finally:
                    await resolver.close()

    async def test_resolver_returns_only_validated_connection_addresses(self):
        answers = [public_address()]
        with patch("src.sources.share_links.ThreadedResolver.resolve", new_callable=AsyncMock, return_value=answers):
            resolver = PublicResolver()
            try:
                self.assertIs(await resolver.resolve("share.google", 443), answers)
            finally:
                await resolver.close()

    async def test_timeout_becomes_user_facing_error(self):
        with patch("src.sources.share_links._fetch_redirect", new_callable=AsyncMock, side_effect=TimeoutError):
            with self.assertRaises(ShareLinkError):
                await resolve_share_url("https://share.google/Ab123")

    async def test_total_deadline_is_enforced(self):
        async def delayed(*args):
            await asyncio.sleep(1)
        with patch("src.sources.share_links.TIMEOUT_SECONDS", 0.01), patch(
            "src.sources.share_links._fetch_redirect", new_callable=AsyncMock, side_effect=delayed
        ):
            with self.assertRaises(ShareLinkError):
                await resolve_share_url("https://share.google/Ab123")

    async def test_final_site_with_private_dns_is_rejected(self):
        with patch("src.sources.share_links._fetch_redirect", new_callable=AsyncMock, return_value="https://ficbook.net/readfic/1"), patch(
            "src.sources.share_links.ThreadedResolver.resolve", new_callable=AsyncMock, return_value=[public_address("10.0.0.1")]
        ):
            with self.assertRaises(ShareLinkError):
                await resolve_share_url("https://share.google/Ab123")

    async def test_nonredirect_html_is_not_executed_or_followed(self):
        from unittest.mock import Mock
        context = AsyncMock()
        context.__aenter__.return_value = SimpleNamespace(status=200, headers={})
        session = SimpleNamespace(get=Mock(return_value=context))
        with self.assertRaises(ShareLinkError):
            await _fetch_redirect(session, "https://share.google/Ab123")

    async def test_fetch_disables_redirects_and_limits_response(self):
        class Content:
            async def iter_chunked(self, size):
                yield b"x" * (MAX_RESPONSE_BYTES + 1)
        response = SimpleNamespace(status=302, headers={"Location": "https://ficbook.net/readfic/1"}, content=Content())
        context = AsyncMock()
        context.__aenter__.return_value = response
        from unittest.mock import Mock
        session = SimpleNamespace(get=Mock(return_value=context))
        with self.assertRaises(ShareLinkError):
            await _fetch_redirect(session, "https://share.google/Ab123")
        session.get.assert_called_once_with("https://share.google/Ab123", allow_redirects=False)
