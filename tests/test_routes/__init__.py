"""tests/test_routes/ package: per-route-module test buckets.

See #183. The 5000-line tests/test_routes.py was split into one
file per mimir/web/routes/ submodule (plus test_cross_cutting.py
for shared-header / kbd-nav / security tests that span every
route). Shared helpers live in _helpers.py.

Test count is preserved against the pre-split file; see the
pytest --collect-only sanity check in the PR description.
"""
