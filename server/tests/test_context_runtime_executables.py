from __future__ import annotations

import pytest

from app.services.context_builder import safe_context_text


@pytest.mark.parametrize(
    "text",
    [
        "Use /usr/bin/chromium and /usr/bin/chromedriver for the requested browser test.",
        "Browser: `/usr/bin/chromium`; driver: `/usr/bin/chromedriver`.",
        "Use (/usr/bin/chromium) and [/usr/bin/chromedriver].",
    ],
)
def test_known_public_executable_references_survive_instruction_redaction(text: str) -> None:
    assert safe_context_text(text) == text


@pytest.mark.parametrize(
    "path",
    [
        "/usr/bin/chromium-private",
        "/usr/bin/chromium.secret",
        "/usr/bin/chromium/private.txt",
        "/usr/bin/chromedriver/token",
        "/usr/bin/chromium/../../private.txt",
        "/usr/bin/../bin/chromium",
        "/usr/bin/Chromium",
        "/USR/bin/chromedriver",
        "/usr/bin/chrоmium",  # Cyrillic o is not an approved executable reference.
        "/usr/bin/chromium\u200b",
        "/usr/bin/chromedriver%2fsecret",
        "/usr/bin/chromedriver?token=secret-value",
        "/home/alice/private.txt",
        "/etc/secret.conf",
        "/usr/local/bin/chromium",
        "/usr/bin/python3",
    ],
)
def test_reference_exception_does_not_expand_to_other_paths(path: str) -> None:
    rendered = safe_context_text(f"Read {path}")
    assert path not in rendered
    assert "/usr/" not in rendered
    assert "private.txt" not in rendered
    assert "secret-value" not in rendered
    assert "<protected-path>" in rendered


@pytest.mark.parametrize(
    "text",
    [
        "password=/usr/bin/chromium",
        'api_key="/usr/bin/chromedriver"',
        "token='/usr/bin/chromium'",
        "secret=/usr/bin/chromium,/home/alice/private.txt",
        "Authorization: Bearer /usr/bin/chromedriver",
        "-----BEGIN PRIVATE KEY-----\n/usr/bin/chromium\n-----END PRIVATE KEY-----",
    ],
)
def test_secret_filter_runs_before_public_executable_exception(text: str) -> None:
    rendered = safe_context_text(text)
    assert "/usr/bin/" not in rendered
    assert "<redacted-secret>" in rendered


def test_public_reference_keeps_private_paths_redacted_without_claiming_availability() -> None:
    rendered = safe_context_text(
        "If available, use /usr/bin/chromium with /usr/bin/chromedriver. "
        "Do not inspect /home/alice/private.txt or /etc/private.conf; password=hidden-value"
    )
    assert rendered.startswith("If available, use /usr/bin/chromium with /usr/bin/chromedriver. ")
    assert rendered.count("<protected-path>") == 2
    assert rendered.endswith("<redacted-secret>")
    assert "private" not in rendered
    assert "hidden-value" not in rendered


def test_short_context_still_filters_credentials_before_bounding() -> None:
    rendered = safe_context_text("password=/usr/bin/chromium trailing text", max_chars=12)
    assert rendered == "<redacted-s…"
