"""Makes ``tests/unit`` a package, which is only about names.

mypy names a file by walking up folders until it finds one without an
``__init__.py``. Without this file, ``tests/unit/conftest.py`` and
``tests/integration/conftest.py`` both come out as a module called ``conftest``, and
mypy refuses to check either. With it, this one is ``unit.conftest``.

``tests/integration`` deliberately stays a plain folder: its tests import their
sibling ``corpus`` by bare name, which only works while that folder is not a
package. Pytest itself is happy either way.
"""
