"""
Test package for the RecoverAI backend.

This file exists so that ``tests`` is a real package rather than a loose folder
of scripts. Two concrete benefits, both of which have bitten this layout before:

1.  Test modules get fully-qualified names (``tests.test_policy_engine``), so two
    files in different directories may share a basename without pytest's
    rootdir-relative import mode colliding on them.
2.  Helpers defined in ``conftest`` are importable as ``tests.conftest`` when a
    test needs a module-level constant rather than a fixture.
"""
