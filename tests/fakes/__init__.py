"""Shared, reusable test doubles for the PR2 execution-gateway test suite.

Not collected by pytest as a test module (no ``test_`` prefix on this
package or the files in it).

Import as ``from fakes.fake_execution_broker import ...`` (not
``tests.fakes...``) -- this environment has an unrelated, globally
installed package also named ``tests`` in ``site-packages`` that shadows
the local ``tests/`` directory for namespace-package resolution (PEP 420:
a real package anywhere on ``sys.path`` always wins over a directory with
no ``__init__.py``, regardless of ``sys.path`` order). pytest's own
rootless "prepend" import mode already puts this ``tests/`` directory
itself on ``sys.path`` when collecting a test file with no
``tests/__init__.py`` present, which is what makes the unprefixed
``fakes.*`` import resolve correctly.
"""
