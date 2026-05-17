"""tests/test_cli/ package: per-cli-module test buckets.

See #238. The 1575-line tests/test_cli.py was split into one
file per mimir/cli/ submodule (plus tests/test_cli/
test_cross_cutting.py for the small surface that tests
`mimir.cli.__init__` itself, register_cli + entry-point smoke
+ configure_logging). Shared helpers live in _helpers.py.

The existing tests/test_cli_backfill_*.py files stay flat
where they already follow the "one file per command group"
shape this split aims for.
"""
