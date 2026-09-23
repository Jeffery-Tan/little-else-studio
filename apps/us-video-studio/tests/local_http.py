"""Use the same loopback Host as the installed Studio in HTTP tests."""
from fastapi.testclient import TestClient as _TestClient


class TestClient(_TestClient):
    def __init__(self, *args, base_url: str = 'http://127.0.0.1', **kwargs):
        super().__init__(*args, base_url=base_url, **kwargs)
