"""config.load_config's parsing of the two /poststats access-token settings:
POST_STATS_ACCESS_TOKEN (a plain string) and POST_STATS_SCOPED_TOKENS (a JSON object of
{"token": "chat_ref"} pairs, each locked to exactly one chat -- see post_stats_web.py).
"""

import os
import unittest
from unittest.mock import patch

from config import load_config
from errors import ChatSummaryError

_REQUIRED_ENV = {
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "deadbeef",
    "OPENAI_API_KEY": "sk-test",
}


class PostStatsTokenConfigTests(unittest.TestCase):
    def _load(self, **extra_env):
        env = dict(_REQUIRED_ENV, **extra_env)
        with patch.dict(os.environ, env, clear=True):
            return load_config()

    def test_unset_scoped_tokens_defaults_to_empty_dict(self):
        cfg = self._load()
        self.assertEqual(cfg.post_stats_scoped_tokens, {})
        self.assertIsNone(cfg.post_stats_access_token)

    def test_access_token_is_read_verbatim(self):
        cfg = self._load(POST_STATS_ACCESS_TOKEN="  mytoken  ")
        self.assertEqual(cfg.post_stats_access_token, "mytoken")

    def test_valid_scoped_tokens_json_parses(self):
        cfg = self._load(
            POST_STATS_SCOPED_TOKENS='{"friendtok": "@theirgroup", "othertok": "-1001234"}'
        )
        self.assertEqual(
            cfg.post_stats_scoped_tokens,
            {"friendtok": "@theirgroup", "othertok": "-1001234"},
        )

    def test_invalid_json_raises(self):
        with self.assertRaises(ChatSummaryError):
            self._load(POST_STATS_SCOPED_TOKENS="{not valid json")

    def test_non_object_json_raises(self):
        with self.assertRaises(ChatSummaryError):
            self._load(POST_STATS_SCOPED_TOKENS='["friendtok", "@theirgroup"]')

    def test_non_string_value_raises(self):
        with self.assertRaises(ChatSummaryError):
            self._load(POST_STATS_SCOPED_TOKENS='{"friendtok": 12345}')

    def test_non_string_key_raises(self):
        with self.assertRaises(ChatSummaryError):
            self._load(POST_STATS_SCOPED_TOKENS='{"1": {"nested": "object"}}')

    def test_blank_scoped_tokens_is_same_as_unset(self):
        cfg = self._load(POST_STATS_SCOPED_TOKENS="   ")
        self.assertEqual(cfg.post_stats_scoped_tokens, {})


if __name__ == "__main__":
    unittest.main()
