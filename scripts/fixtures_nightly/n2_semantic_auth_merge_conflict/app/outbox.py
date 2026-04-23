from __future__ import annotations

EMAIL_OUTBOX: list[dict[str, str]] = []


def clear() -> None:
    EMAIL_OUTBOX.clear()


def send_mail(to: str, subject: str, body: str) -> None:
    EMAIL_OUTBOX.append({"to": to, "subject": subject, "body": body})
