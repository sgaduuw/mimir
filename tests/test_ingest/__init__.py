"""tests/test_ingest/ package: per-ingest-module test buckets.

See #235. The 2000-line tests/test_ingest.py was split into one
file per mimir/ingest/ submodule (plus test_cross_cutting.py for
alembic migration tests that span modules). Shared helpers live
in _helpers.py.

Test count is preserved against the pre-split file; see the
pytest --collect-only sanity check in the PR description.
"""
