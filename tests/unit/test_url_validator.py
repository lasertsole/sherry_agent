# pyright: reportArgumentType=false
# pyright: reportUnknownParameterType=false
# pyright: reportAny=false
# pyright: reportMissingParameterType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
"""Unit tests for pub_func/validator/is_url.py."""

from pub_func.validator.is_url import is_url


class TestIsUrl:
    def test_https_valid(self):
        assert is_url("https://example.com/path?a=1#frag") is True

    def test_http_valid(self):
        assert is_url("http://example.com") is True

    def test_ftp_valid(self):
        assert is_url("ftp://files.example.com/x") is True

    def test_ws_wss_valid(self):
        assert is_url("ws://socket.example.com") is True
        assert is_url("wss://socket.example.com") is True

    def test_file_with_netloc_valid(self):
        assert is_url("file://localhost/etc/hosts") is True

    def test_file_without_netloc_invalid(self):
        # file: scheme requires a non-empty netloc
        assert is_url("file:///etc/hosts") is False

    def test_mailto_valid(self):
        assert is_url("mailto:user@example.com") is True

    def test_tel_valid(self):
        assert is_url("tel:+1234567890") is True

    def test_sms_valid(self):
        assert is_url("sms:+1234567890") is True

    def test_missing_scheme(self):
        assert is_url("example.com") is False
        assert is_url("not-a-url") is False

    def test_unrecognized_scheme(self):
        assert is_url("gopher://example.com") is False
        assert is_url("javascript:alert(1)") is False

    def test_http_requires_netloc(self):
        # scheme in netloc-requiring set but missing host
        assert is_url("http:///path") is False
        assert is_url("https://") is False

    def test_empty_string(self):
        assert is_url("") is False

    def test_whitespace_only(self):
        assert is_url("   ") is False

    def test_non_string_input(self):
        # deliberately out-of-contract values; isinstance guard returns False
        assert is_url(None) is False
        assert is_url(12345) is False
        assert is_url(b"https://example.com") is False

    def test_padded_value_stripped(self):
        assert is_url("  https://example.com  ") is True

    def test_embedded_control_char_does_not_raise(self):
        # Python 3.13 urlparse tolerates embedded control chars in netloc
        # without raising; the whitelisted scheme + non-empty netloc keeps it valid
        assert is_url("https://example.com\x00evil") is True

    def test_data_uri(self):
        # data: requires only scheme + path
        assert is_url("data:text/plain;base64,SGVsbG8=") is True
