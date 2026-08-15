import json
import logging

from whatsapp_voice import logging_setup


class TestRedact:
    def test_masks_meta_access_token(self):
        out = logging_setup.redact("token=EAAGm0PX4ZCpsBO1abcdef12345 rest")
        assert "EAAGm0PX4ZCpsBO1abcdef12345" not in out
        assert "[REDACTED-TOKEN]" in out

    def test_masks_anthropic_key(self):
        out = logging_setup.redact("key sk-ant-api03-AbCdEf123456789 here")
        assert "sk-ant-api03-AbCdEf123456789" not in out

    def test_phone_number_keeps_only_last_four(self):
        out = logging_setup.redact("Denied 919876543210 today")
        assert "919876543210" not in out
        assert "3210" in out

    def test_leaves_ordinary_text_alone(self):
        assert logging_setup.redact("Transcribed in 1.2s: hello") == \
            "Transcribed in 1.2s: hello"

    def test_short_numbers_are_untouched(self):
        # Counts, ports and durations must stay readable.
        assert logging_setup.redact("total_campaigns=12 port=8000") == \
            "total_campaigns=12 port=8000"


class TestPinRedaction:
    """The real protection is that `PaymentFlow` never hands a PIN to a logger.
    This is the backstop for a careless call site added later."""

    def test_masks_a_pin_after_the_word_pin(self):
        out = logging_setup.redact("user said pin 1234 aloud")
        assert "1234" not in out
        assert "[REDACTED-PIN]" in out

    def test_masks_a_spaced_pin(self):
        assert "[REDACTED-PIN]" in logging_setup.redact("pin: 1 2 3 4")

    def test_masks_a_six_digit_pin(self):
        out = logging_setup.redact("PIN=123456")
        assert "123456" not in out

    def test_masks_devanagari_pin_label(self):
        out = logging_setup.redact("पिन 1234")
        assert "1234" not in out

    def test_masks_an_otp(self):
        assert "[REDACTED-PIN]" in logging_setup.redact("otp 445566")

    def test_a_pin_long_enough_to_look_like_a_phone_is_still_fully_masked(self):
        """Order matters: the phone rule keeps the last four digits, which for a
        PIN would be the whole thing."""
        out = logging_setup.redact("pin 12345678")
        assert "5678" not in out

    def test_amounts_are_not_mistaken_for_pins(self):
        """Money figures have to stay readable in the log — they are how a
        transfer is audited."""
        assert logging_setup.redact("amount_paise=200000") == "amount_paise=200000"

    def test_intent_ids_survive(self):
        line = "Intent a1b2c3d4e5f6 completed, ref=TXNABCD1234"
        assert logging_setup.redact(line) == line


class TestRedactingFilterInPipeline:
    def test_lazy_args_are_redacted_too(self, caplog):
        """A number passed as a %s argument must not slip through."""
        logging_setup.configure(level="INFO", fmt="text")
        logger = logging.getLogger("test.redaction")

        records = []
        handler = logging.getLogger().handlers[0]

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        capture = Capture()
        capture.addFilter(logging_setup.RedactingFilter())
        logging.getLogger().addHandler(capture)
        try:
            logger.info("Denied %s", "919876543210")
        finally:
            logging.getLogger().removeHandler(capture)
            assert handler is not None

        assert records
        assert "919876543210" not in records[0]


class TestJsonFormatter:
    def test_emits_one_json_object(self):
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="p", lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        parsed = json.loads(logging_setup.JsonFormatter().format(record))
        assert parsed["message"] == "hello"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "x"
        assert "time" in parsed

    def test_includes_exception_when_present(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="x", level=logging.ERROR, pathname="p", lineno=1,
                msg="failed", args=(), exc_info=sys.exc_info(),
            )
        parsed = json.loads(logging_setup.JsonFormatter().format(record))
        assert "ValueError: boom" in parsed["exception"]


class TestConfigure:
    def test_installs_stdout_and_ring_buffer_handlers(self):
        logging_setup.configure()
        handlers = logging.getLogger().handlers
        assert len(handlers) == 2
        assert isinstance(handlers[0], logging.StreamHandler)
        assert isinstance(handlers[1], logging_setup.RingBufferHandler)

    def test_is_idempotent(self):
        """A reload must not double every log line."""
        logging_setup.configure()
        logging_setup.configure()
        assert len(logging.getLogger().handlers) == 2

    def test_json_format_selected(self):
        logging_setup.configure(fmt="json")
        assert isinstance(
            logging.getLogger().handlers[0].formatter, logging_setup.JsonFormatter
        )

    def test_level_is_applied(self):
        logging_setup.configure(level="WARNING")
        assert logging.getLogger().level == logging.WARNING
        logging_setup.configure(level="INFO")  # restore for other tests

    def test_noisy_http_loggers_are_quietened(self):
        """httpx logs full signed media URLs at INFO — not useful, not retainable."""
        logging_setup.configure()
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_ring_buffer_is_reachable_after_configure(self):
        logging_setup.configure()
        assert logging_setup.get_ring_buffer() is not None

    def test_ring_buffer_is_none_before_configure(self, monkeypatch):
        monkeypatch.setattr(logging_setup, "_ring_buffer", None)
        assert logging_setup.get_ring_buffer() is None


class TestRingBufferHandler:
    def test_captures_a_record(self):
        handler = logging_setup.RingBufferHandler()
        logger = logging.getLogger("test.ringbuffer")
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            logger.info("hello %s", "world")
        finally:
            logger.removeHandler(handler)
            # Otherwise this persists on the cached logger object for the rest
            # of the test session and silently swallows records from any test
            # that logs through a "test.ringbuffer.*" child logger later on.
            logger.propagate = True

        [entry] = handler.snapshot()
        assert entry["message"] == "hello world"
        assert entry["level"] == "INFO"
        assert entry["logger"] == "test.ringbuffer"
        assert "time" in entry

    def test_oldest_entries_drop_once_full(self):
        handler = logging_setup.RingBufferHandler(maxlen=3)
        logger = logging.getLogger("test.ringbuffer.bounded")
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            for i in range(5):
                logger.info("line %d", i)
        finally:
            logger.removeHandler(handler)
            logger.propagate = True

        messages = [e["message"] for e in handler.snapshot()]
        assert messages == ["line 2", "line 3", "line 4"]

    def test_redaction_from_the_stdout_handler_carries_over(self):
        """Handlers on one logger share a LogRecord — the stdout handler's
        RedactingFilter (added first, in configure()) must already have
        mutated record.msg by the time this handler's emit() runs."""
        logging_setup.configure()
        logger = logging.getLogger("test.ringbuffer.redaction")
        logger.info("Denied %s", "919876543210")

        entries = logging_setup.get_ring_buffer().snapshot()
        assert "919876543210" not in entries[-1]["message"]
