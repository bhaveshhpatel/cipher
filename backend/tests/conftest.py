
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def _reset_signal_memory():
    """
    Clear signal_store in-memory state before every test.

    This prevents test-ordering bugs where a prior test that called
    save_signal() with _client=None populates _signal_memory, causing
    a later test that expects an empty list from get_recent_signals()
    to fail.
    """
    from services import signal_store
    signal_store._clear_signal_memory()
    yield
    signal_store._clear_signal_memory()
