"""A stub that makes importing ``gi`` fail, the way a CI runner without GTK does.

``scripts/release-gate`` puts this directory on ``PYTHONPATH`` ahead of ``app/src``
and runs the test suite a second time. Every UI test then exercises its
``unittest.skipIf`` guard instead of touching GTK, so a test class that forgot the
guard fails here, on the release gate, rather than in CI at release time.
"""

raise ImportError("gi is not available: GTK bindings blocked for CI parity")
