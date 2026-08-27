import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: needs the mock DMS running on :8080 (start with `pi dms`)"
    )


@pytest.fixture
def sample_form_url() -> str:
    return (FIXTURES / "sample_form.html").resolve().as_uri()
