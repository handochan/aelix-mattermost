"""Pairing: an unknown user DMs the bot, gets a code, and an admin approves the code."""

from __future__ import annotations

import secrets

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
CODE_LENGTH = 8
CODE_TTL = 3600.0  # seconds a code stays valid
MAX_PENDING = 50  # unexpired codes at once; more requests are refused
REPLY_INTERVAL = 600.0  # an unknown user gets at most one reply this often
DENY_BLOCK = 86400.0  # a denied user gets no new code for this long


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalize(code: str) -> str:
    """"abcd-efgh" -> "ABCDEFGH"; the dash is only for reading."""
    return "".join(c for c in code.upper() if c.isalnum())


def shown(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LENGTH else code


def request_message(code: str, minutes: int) -> str:
    return (f"이 봇을 사용하려면 관리자 승인이 필요합니다. 관리자에게 승인 코드 `{shown(code)}`를 "
            f"전달해주세요. 코드는 {minutes}분 동안 유효합니다.")


def waiting_message(code: str) -> str:
    return f"관리자 승인을 기다리고 있습니다. 승인 코드: `{shown(code)}`"


FULL = "지금은 새 사용 승인 요청을 받을 수 없습니다. 잠시 후 다시 시도해주세요."
APPROVED = "봇 사용이 승인되었습니다. 이제 DM으로 질문하거나 채널에서 멘션해 사용할 수 있습니다. `!help`로 사용법을 확인하세요."


def admin_notice(username: str, code: str) -> str:
    return (f"새 봇 사용 요청: @{username}\n승인: `!pair approve {shown(code)}` · "
            f"거절: `!pair deny {shown(code)}`")
