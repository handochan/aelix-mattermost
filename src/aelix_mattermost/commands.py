"""Bot commands typed as messages ("!new", or "/new" after a mention or a leading space)."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Alias -> command. Unknown names are not commands: "/etc/hosts" stays a prompt.
NAMES = {
    "help": "help", "new": "new", "reset": "new", "stop": "stop", "cancel": "stop",
    "steer": "steer", "queue": "queue", "status": "status", "model": "model", "usage": "usage",
    "compact": "compact", "tools": "tools", "pair": "pair",
}
_COMMAND = re.compile(r"[!/]([A-Za-z]+)(?:[ \t\r\n]+(.*))?", re.DOTALL)


@dataclass(frozen=True)
class Command:
    name: str  # canonical name
    args: str = ""


def parse_command(text: str) -> Command | None:
    """A command message, or None. Mattermost clients take a leading "/" for their own
    slash commands, so in a DM "/new" is typed as " /new" (or "!new")."""
    match = _COMMAND.fullmatch(text.strip())
    if match is None:
        return None
    name = NAMES.get(match[1].lower())
    return None if name is None else Command(name, (match[2] or "").strip())


HELP = (
    "**Aelix Mattermost**\n"
    "DM으로 질문하거나 채널·그룹 DM에서 `@{bot} 질문`을 보내세요. 후속 대화는 스레드에서 이어가세요.\n"
    "파일을 첨부하면 함께 전달됩니다.\n\n"
    "**명령** (`!명령` 또는 앞에 공백을 넣은 ` /명령`{slash})\n"
    "- `!status`: 이 대화의 상태 · `!usage`: 토큰·비용 사용량\n"
    "- `!stop`: 내 요청 중단(실행 중·대기 중) · `!new`: 이 대화의 모델 컨텍스트 초기화\n"
    "- `!steer 내용`: 실행 중인 작업에 지시 끼워넣기 · `!queue 내용`: 앞 작업이 끝난 뒤 실행\n"
    "- `!model`: 현재 모델 (`!model 이름`: 변경) · `!tools`: 사용할 수 있는 도구\n"
    "- `!compact`: 대화 컨텍스트 압축 · `!help`: 이 도움말\n"
    "실행 중에 새 메시지를 보내면 {busy}"
)
BUSY_HELP = {
    "steer": "진행 중인 작업에 끼워 넣어 반영합니다.",
    "interrupt": "진행 중인 작업을 멈추고 새 메시지를 처리합니다.",
    "queue": "앞 작업이 끝난 뒤 차례로 처리합니다.",
}
ADMIN_HELP = "\n\n**관리자**: `!pair` 대기 목록 · `!pair approve 코드` · `!pair deny 코드` · `!pair revoke @사용자`"


def help_text(bot: str, busy_mode: str, slash_trigger: str | None, admin: bool) -> str:
    slash = f", 또는 `/{slash_trigger} 명령`" if slash_trigger else ""
    text = HELP.format(bot=bot, slash=slash, busy=BUSY_HELP[busy_mode])
    return text + (ADMIN_HELP if admin else "")
