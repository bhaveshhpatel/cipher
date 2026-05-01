"""
backend/tests/integration/conftest.py

Shared pytest configuration for Apex integration tests.

Provides:
- pytest markers for path coverage scenarios
- async event loop policy
- environment guards to prevent production I/O during test runs
"""

import os
import pytest


# ---------------------------------------------------------------------------
# Prevent any real I/O from activating during integration tests
# ---------------------------------------------------------------------------
os.environ.setdefault("CIPHER_ENV", "test")
os.environ.setdefault("SUPABASE_URL", "http://test.invalid")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("TRADIER_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# Custom markers
# ---------------------------------------------------------------------------
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "qa_path(id): marks a test as belonging to a specific QA path scenario (e.g. QA-10)",
    )
    config.addinivalue_line(
        "markers",
        "ci_gate: marks a test as a CI hard-gate invariant — must never regress",
    )
    config.addinivalue_line("markers", "apex_l1: test exercises Apex L1 signal gate")
    config.addinivalue_line("markers", "apex_l2: test exercises Apex L2 accumulator")
    config.addinivalue_line("markers", "apex_l3: test exercises Apex L3 composite scorer")
    config.addinivalue_line("markers", "apex_l4: test exercises Apex L4 ladder detector")
    config.addinivalue_line("markers", "parser: test exercises Layer 2 parser")
    config.addinivalue_line("markers", "dedup: test exercises Layer 3 dedup cache")


# ---------------------------------------------------------------------------
# Async event loop
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def event_loop_policy():
    """Use default asyncio policy for integration tests."""
    import asyncio
    return asyncio.DefaultEventLoopPolicy()
