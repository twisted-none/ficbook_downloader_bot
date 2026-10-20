# Verification: 2026-10-16

Scope: approved hardening plan, excluding the change of Docker user. No production
deployment, real source login, Telegram polling, or production data cleanup.

## Checks

- Full suite: 267 tests pass without skips in Python 3.14.8 on Linux x86_64,
  including 10 PostgreSQL integration tests against an isolated temporary DB.
- Docker image builds from the existing Dockerfile; pip check succeeds.
- pip-audit 2.10.1 reports no known vulnerabilities in requirements.lock.
- All 500 jobs in a synthetic queue drain without leaked capacity. Local Windows
  measurement: admission 0.068 seconds, total 3.295 seconds, no network operations.
  This measures scheduling, not real source-download throughput.
- Code graph refreshed: 1300 nodes, 4211 edges. Document semantics and community
  labels were not rebuilt with an LLM.

## Review Fixes

- Share the five-start turn across Ficbook account queues, preserve idle-account
  use, freeze fair rounds against returning/new priority traffic, and discard
  scheduling history when idle.
- Hold capacity through file upload and until cancelled worker threads exit.
- Preserve safe FanFicFare transport and cookies after fetcher recreation; handle
  the www.wattpad.com site key.
- Count downloaded chapters at ingestion, not again for each exported format.
- Use AO3 chapter fallback when the whole HTML export exceeds the response limit.
- Reject truncated Content-Length responses, oversized/decompressed HTTP bodies
  and excessive cover dimensions; retain verified TLS and public-IP pinning.
- Bound initial status retries and error notices, including persistent outages.
- Reject expired/old settings callbacks without modifying another session's UI.
- Preserve existing users.json on malformed input or interrupted replacement;
  fsync file and parent directory on POSIX.

## Remaining Operational Checks

- Real source authentication, site responses and Telegram delivery require a
  separate live check. Tests mock these integrations; no real credentials read.
- Limits are per-process and do not impose a hard OS RSS limit. The overall
  processing deadline is cooperative around native CPU work and DNS resolution.
- Telegram send retries cannot guarantee exactly-once delivery after an ambiguous
  network timeout. They do not replay the entire download.
- Changes remain local; Git publication and server deployment were not performed.
- Docker remains root as explicitly requested.
