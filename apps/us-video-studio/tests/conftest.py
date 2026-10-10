"""Test bootstrap.

The tests import ``studio`` from the app directory. Nothing here reaches the
network, and no test may require a model credential.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
# Optional business inputs are independent of the disposable knowledge snapshot.
os.environ.pop('STUDIO_BUSINESS_ROOT', None)
for path in (APP_ROOT, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from studio import paths
paths.KNOWLEDGE_ROOT = TESTS_DIR / 'fixtures/business'
