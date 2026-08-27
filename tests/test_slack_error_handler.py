"""Regression tests for the in-app ERROR-log -> Slack forwarder (audit._SlackErrorHandler).

Verifies the behaviour Anita asked for — the service's error logs reach Slack — without any
network calls and without regressing normal logging:
  * ERROR/CRITICAL records are enqueued for delivery; INFO/WARNING are not.
  * NO webhook configured -> nothing is sent (safe default; nothing changes until the env is set).
  * The same message is throttled within the cooldown; distinct messages are not.
  * A traceback is attached when present.
  * The handler is attached to the root logger (so behaviour + ml.* both feed it).

Determinism: we swap in a fresh queue so the module's background delivery thread (blocked on the
ORIGINAL queue) cannot drain what we enqueue, and we drive emit() directly — no sleeps, no sockets.

Run standalone:  .venv-ml/bin/python tests/test_slack_error_handler.py
Or with pytest:  pytest tests/test_slack_error_handler.py
"""
import logging
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root on path

import audit

_ORIG_URL = audit._slack_url


def _reset(url: str):
    """Fresh queue + throttle state, and a fixed webhook value for this case."""
    audit._SLACK_Q = queue.Queue(maxsize=200)   # background worker is blocked on the OLD queue
    audit._SLACK_LAST = {}
    audit._slack_url = (lambda: url)


def _restore():
    audit._slack_url = _ORIG_URL


def _rec(level, msg, name="behaviour", exc_info=None):
    return logging.LogRecord(name=name, level=level, pathname="/app/service.py",
                             lineno=42, msg=msg, args=(), exc_info=exc_info)


def _emit(record):
    h = audit._SlackErrorHandler()
    h.setLevel(logging.ERROR)
    h.emit(record)


def test_no_webhook_sends_nothing():
    _reset("")
    try:
        _emit(_rec(logging.ERROR, "db exploded"))
        assert audit._SLACK_Q.qsize() == 0
    finally:
        _restore()


def test_error_is_enqueued_with_context():
    _reset("https://hooks.slack.test/xyz")
    try:
        _emit(_rec(logging.ERROR, "db exploded", name="ml.serve"))
        assert audit._SLACK_Q.qsize() == 1
        text = audit._SLACK_Q.get_nowait()
        assert "behaviour-profile ERROR" in text
        assert "db exploded" in text
        assert "ml.serve" in text
    finally:
        _restore()


def test_warning_is_ignored():
    _reset("https://hooks.slack.test/xyz")
    try:
        _emit(_rec(logging.WARNING, "just a warning"))
        assert audit._SLACK_Q.qsize() == 0
    finally:
        _restore()


def test_same_message_is_throttled():
    _reset("https://hooks.slack.test/xyz")
    try:
        _emit(_rec(logging.ERROR, "repeated failure"))
        _emit(_rec(logging.ERROR, "repeated failure"))   # within cooldown -> dropped
        assert audit._SLACK_Q.qsize() == 1
    finally:
        _restore()


def test_distinct_messages_both_sent():
    _reset("https://hooks.slack.test/xyz")
    try:
        _emit(_rec(logging.ERROR, "failure A"))
        _emit(_rec(logging.ERROR, "failure B"))
        assert audit._SLACK_Q.qsize() == 2
    finally:
        _restore()


def test_traceback_is_attached():
    _reset("https://hooks.slack.test/xyz")
    try:
        try:
            raise ValueError("kaboom")
        except Exception:
            ei = sys.exc_info()
        _emit(_rec(logging.ERROR, "handler failed", exc_info=ei))
        text = audit._SLACK_Q.get_nowait()
        assert "```" in text and "ValueError" in text
    finally:
        _restore()


def test_handler_attached_to_root_logger():
    assert any(isinstance(h, audit._SlackErrorHandler) for h in logging.getLogger().handlers)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
