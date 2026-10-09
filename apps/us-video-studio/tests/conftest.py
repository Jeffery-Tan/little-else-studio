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
# Offline tests use a versioned snapshot, never the operator's live business library.
os.environ['STUDIO_BUSINESS_ROOT'] = str(TESTS_DIR / 'fixtures/business')
for path in (APP_ROOT, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
