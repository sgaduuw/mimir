"""Broker test package: shared `_helpers.py` lives next to this file.

## Mock-vs-real broker policy

Three patterns appear across broker tests; pick the right one for
what you're pinning:

1. **Real WriterThread + ReadSessionPool, no socket.** Use the
   session-scoped `broker_active` fixture (conftest.py) or call
   `WriterThread.from_settings()` directly. Right for tests that
   exercise the in-process WriteOp dispatch contract — `_submit_*`
   helpers, closure atomicity, cursor invariants, BEGIN IMMEDIATE
   semantics. The Phase 3/3b/3c/4/5 phase-test files
   (`tests/test_ingest_phase*.py`, `tests/test_mainline_phase3.py`)
   live here.

2. **Real broker server + real UNIX socket + real client.** Use
   `broker_running(short_socket_path(...))` from
   `tests/test_broker/_helpers.py`. Right for tests that exercise
   the wire path: RPC framing, multi-client concurrency, server
   hardening, the protocol's request/reply round-trip. Slower
   (broker spin-up takes ~tens of ms) so reserve for tests that
   actually NEED the socket.

3. **FakeBrokerClient mock.** A stub class that records dispatches
   without spinning a real broker. Right for CLI tests that pin
   "this command dispatches RPC X with these args" (the dispatch-
   shape contract). Fastest; correct when the CLI test isn't trying
   to verify what the broker DOES with the call, only that the
   call was MADE.

A test that picks pattern 3 when the contract actually depends on
the broker's behavior (e.g. atomicity, real timeout semantics)
will pass under a regression that the real broker would have
caught. When in doubt, prefer 1 or 2.

Bidirectional symmetry: if a test asserts something about the
BROKER's behavior, pattern 1 or 2; if it asserts something about
the CALLER's dispatch shape, pattern 3 is fine.
"""
