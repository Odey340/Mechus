"""Keep-alive HTTP pool: pools are reused across calls for the same host."""

from __future__ import annotations

import unittest

from koov.http_pool import _POOLS, get_pool_for_url, http_request


class TestHttpPoolReuse(unittest.TestCase):
    def setUp(self) -> None:
        _POOLS.clear()

    def test_same_host_returns_same_pool(self) -> None:
        a = get_pool_for_url("https://api.openai.com/v1/chat/completions")
        b = get_pool_for_url("https://api.openai.com/v1/embeddings")
        self.assertIs(a, b, "Same host must reuse the same PoolManager")

    def test_different_hosts_get_different_pools(self) -> None:
        a = get_pool_for_url("https://api.openai.com/v1/chat/completions")
        b = get_pool_for_url("https://api.elevenlabs.io/v1/text-to-speech/x/stream")
        self.assertIsNot(a, b, "Different hosts must get distinct PoolManagers")

    def test_http_request_signature(self) -> None:
        self.assertTrue(callable(http_request))


if __name__ == "__main__":
    unittest.main()
