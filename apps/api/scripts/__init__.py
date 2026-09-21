"""Operator commands.

A package rather than a bare directory because `tests/integration/test_reviewer_identity`
imports `scripts.source_review`, and without this marker the same file resolves under two
module names -- which mypy refuses outright, taking the whole `make typecheck-py` gate
down with it. Nothing imports this module for its contents; each script is still run
directly.
"""
