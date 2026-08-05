from whatsapp_voice.whatsapp.signature import compute_signature, is_valid_signature

SECRET = "app-secret-123"
BODY = b'{"entry":[{"id":"1"}]}'


class TestComputeSignature:
    def test_has_sha256_prefix(self):
        assert compute_signature(BODY, SECRET).startswith("sha256=")

    def test_is_deterministic(self):
        assert compute_signature(BODY, SECRET) == compute_signature(BODY, SECRET)

    def test_differs_per_secret(self):
        assert compute_signature(BODY, SECRET) != compute_signature(BODY, "other")


class TestIsValidSignature:
    def test_accepts_correct_signature(self):
        assert is_valid_signature(BODY, compute_signature(BODY, SECRET), SECRET)

    def test_rejects_wrong_secret(self):
        assert not is_valid_signature(BODY, compute_signature(BODY, "other"), SECRET)

    def test_rejects_tampered_body(self):
        header = compute_signature(BODY, SECRET)
        assert not is_valid_signature(b'{"entry":[{"id":"2"}]}', header, SECRET)

    def test_rejects_missing_header(self):
        assert not is_valid_signature(BODY, None, SECRET)

    def test_rejects_header_without_prefix(self):
        bare = compute_signature(BODY, SECRET).removeprefix("sha256=")
        assert not is_valid_signature(BODY, bare, SECRET)

    def test_rejects_empty_header(self):
        assert not is_valid_signature(BODY, "", SECRET)

    def test_byte_exactness_matters(self):
        # Re-serialised JSON has different bytes even though it parses the same.
        # This is why the app must hash the raw body, never a re-encoded dict.
        header = compute_signature(b'{"a":1,"b":2}', SECRET)
        assert not is_valid_signature(b'{"b": 2, "a": 1}', header, SECRET)
