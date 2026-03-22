#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
from itsdangerous import TimestampSigner, BadTimeSignature
from itsdangerous.encoding import base64_encode

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def _int_to_bytes(num):
    if num == 0:
        return b"\x00"
    result = []
    while num:
        result.append(num & 0xFF)
        num >>= 8
    return bytes(reversed(result))


def _make_tampered_token(signer, huge_timestamp_int):
    """Create a properly-formatted token with a huge timestamp."""
    signed = signer.sign(b"test")
    parts = signed.split(b".")
    huge_ts = base64_encode(_int_to_bytes(huge_timestamp_int))
    return parts[0] + b"." + huge_ts + b"." + parts[2]


def check_overflow_raises_bad_time_signature():
    """A token with a huge timestamp should raise BadTimeSignature, not ValueError."""
    signer = TimestampSigner("secret-key")
    tampered = _make_tampered_token(signer, 2**45)
    try:
        signer.unsign(tampered)
        raise AssertionError("Should have raised an exception for huge timestamp")
    except BadTimeSignature:
        pass  # Correct behavior after fix
    except ValueError as e:
        raise AssertionError(f"Got raw ValueError instead of BadTimeSignature: {e}")
    except OSError as e:
        raise AssertionError(f"Got raw OSError instead of BadTimeSignature: {e}")


def check_normal_sign_unsign_works():
    """Normal sign/unsign should still work after the fix."""
    signer = TimestampSigner("secret-key")
    signed = signer.sign("hello")
    value = signer.unsign(signed, max_age=10)
    assert value == b"hello", f"Expected b'hello', got {value!r}"


def check_expired_signature_still_caught():
    """Expired signatures should still raise SignatureExpired."""
    import time
    signer = TimestampSigner("secret-key")
    signed = signer.sign("hello")
    time.sleep(0.1)
    try:
        signer.unsign(signed, max_age=0)
    except Exception as e:
        assert "Signature" in type(e).__name__, \
            f"Expected Signature-related exception, got {type(e).__name__}"


def check_timed_py_has_valueerror_handling():
    """The timed.py unsign method should catch ValueError from timestamp_to_datetime."""
    source = open("src/itsdangerous/timed.py").read()
    assert "ValueError" in source, \
        "timed.py should handle ValueError"
    assert "Malformed" in source or "malformed" in source, \
        "timed.py should mention Malformed timestamp"


report("Overflow timestamp raises BadTimeSignature", check_overflow_raises_bad_time_signature)
report("Normal sign/unsign still works", check_normal_sign_unsign_works)
report("Expired signatures still caught properly", check_expired_signature_still_caught)
report("timed.py has ValueError handling", check_timed_py_has_valueerror_handling)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
