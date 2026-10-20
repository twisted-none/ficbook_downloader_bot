# Remaining Hardening

Approved scope: all stages below; Docker continues to run as root. No production
deployment, real account access, live security probes, or production data cleanup.

1. Share fair scheduling across site/account queues: five starts per user turn,
   work-conserving when alone, priority without starvation, cancellation/failover.
2. Bound admission, concurrency, HTTP responses, chapters, book/output size and
   processing time. Keep limits configurable and failures understandable.
3. Apply public-IP pinning, verified HTTPS, origin/credential isolation and safe
   redirects to remaining source fetchers without editing dependencies.
4. Retry initial Telegram status sends with bounded backoff; never replay the
   entire download because a notification failed.
5. Expire inactive UI sessions after 30 minutes and handle stale callbacks.
6. Write users.json atomically, fail safely on corruption, move file I/O off the
   event loop.
7. Run focused and full tests, isolated PostgreSQL, Docker build and dependency
   audit. Review changes, document limits and refresh the code graph.

Acceptance: the existing formats, navigation, source logins, queue failover,
settings and all-time analytics remain functional. New behavior has regression
tests. Existing credentials and Docker user are unchanged.
