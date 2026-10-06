"""Tests for bot/log_redaction.py.

The bot token is a full-control credential, and httpx logs every Bot API call
at INFO with the token in the URL path — so it was landing in plaintext in Fly
logs on every sendMessage. These pin the redaction.
"""
from __future__ import annotations

import logging

import pytest

from bot.log_redaction import RedactSecretsFilter, install, redact


# Synthetic, never a real token — it only has to match the shape the filter
# looks for (<digits>:<35+ url-safe chars>). Pasting a live value here would put
# the credential in git history, which is the exact problem this module fixes.
FAKE_TOKEN = "1111111111:AAAAthis-is-a-fake-token-for-tests-AA"


class TestRedact:
    def test_bot_api_url_path(self):
        out = redact(f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage")
        assert FAKE_TOKEN not in out
        assert "api.telegram.org/bot<redacted>/sendMessage" in out

    def test_bare_token(self):
        out = redact(f"TELEGRAM_TOKEN={FAKE_TOKEN}")
        assert FAKE_TOKEN not in out

    def test_structural_match_needs_no_configured_token(self):
        """Dev tokens and rotated tokens must be caught too, so the pattern
        can't depend on knowing the current value."""
        other = "1234567890:ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
        out = redact(f"https://api.telegram.org/bot{other}/getMe")
        assert other not in out

    def test_extra_secret_literal(self):
        out = redact("key is s3cret-value-here", extra_secrets=("s3cret-value-here",))
        assert "s3cret-value-here" not in out

    def test_short_extra_secret_ignored(self):
        """Replacing a 1-2 char 'secret' would shred unrelated log text."""
        out = redact("a normal message", extra_secrets=("a",))
        assert out == "a normal message"

    def test_empty_extra_secret_ignored(self):
        assert redact("unchanged", extra_secrets=("",)) == "unchanged"

    def test_ordinary_text_untouched(self):
        msg = 'HTTP Request: GET https://www.youtube.com/feeds/videos.xml "200 OK"'
        assert redact(msg) == msg

    def test_does_not_eat_timestamps_or_ratios(self):
        """The bare-token pattern is digits:chars — must not match clock times
        or the id:name shapes that show up in ordinary log lines."""
        for msg in ("elapsed 12:30", "ratio 3:4", "position 00000779:4152"):
            assert redact(msg) == msg


class TestRedactSecretsFilter:
    def _record(self, msg, args=()):
        return logging.LogRecord(
            name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
            msg=msg, args=args, exc_info=None,
        )

    def test_redacts_token_living_in_args(self):
        """httpx logs 'HTTP Request: %s %s ...' — the URL is an arg, not the
        msg, so rewriting record.msg alone would leave the token intact."""
        record = self._record(
            "HTTP Request: %s %s",
            ("POST", f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"),
        )
        assert RedactSecretsFilter().filter(record) is True
        assert FAKE_TOKEN not in record.getMessage()
        assert "<redacted>" in record.getMessage()

    def test_clean_record_keeps_lazy_args(self):
        """Untouched records must keep %s-style deferred formatting."""
        record = self._record("fetched %d feeds", (3,))
        assert RedactSecretsFilter().filter(record) is True
        assert record.args == (3,)
        assert record.getMessage() == "fetched 3 feeds"

    def test_never_drops_records(self):
        record = self._record(f"token {FAKE_TOKEN}")
        assert RedactSecretsFilter().filter(record) is True

    def test_malformed_args_do_not_crash(self):
        """A bad format string is the formatter's problem — logging must not
        blow up inside our filter."""
        record = self._record("needs %d args", ("not-an-int",))
        assert RedactSecretsFilter().filter(record) is True

    def test_extra_secret_from_config(self):
        record = self._record("using %s", ("my-super-secret-token",))
        f = RedactSecretsFilter(extra_secrets=("my-super-secret-token",))
        assert f.filter(record) is True
        assert "my-super-secret-token" not in record.getMessage()


class TestInstall:
    @pytest.fixture
    def root_with_handler(self):
        root = logging.getLogger()
        handler = logging.StreamHandler()
        root.addHandler(handler)
        yield root, handler
        root.removeHandler(handler)

    def test_attaches_to_handlers_not_logger(self, root_with_handler):
        """A filter on the root *logger* is never consulted for records
        propagated from child loggers like httpx — which is precisely the case
        we need to cover. It has to sit on the handlers."""
        root, handler = root_with_handler
        install()
        assert any(isinstance(f, RedactSecretsFilter) for f in handler.filters)
        assert not any(isinstance(f, RedactSecretsFilter) for f in root.filters)

    def test_idempotent(self, root_with_handler):
        _, handler = root_with_handler
        install()
        install()
        matching = [f for f in handler.filters if isinstance(f, RedactSecretsFilter)]
        assert len(matching) == 1

    def test_propagated_child_record_is_redacted(self, root_with_handler, caplog):
        """End-to-end: an httpx-style INFO line reaching a root handler comes
        out redacted."""
        _, handler = root_with_handler
        install()
        emitted = []
        handler.emit = lambda record: emitted.append(record.getMessage())
        # Root defaults to WARNING under pytest; an INFO record would be
        # dropped on level before it ever reached a handler.
        caplog.set_level(logging.INFO)

        logging.getLogger("httpx").info(
            "HTTP Request: %s %s",
            "POST",
            f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
        )

        assert emitted, "record never reached the root handler"
        assert all(FAKE_TOKEN not in m for m in emitted)
