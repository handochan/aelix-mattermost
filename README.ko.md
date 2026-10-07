# aelix-mattermost

Mattermost **Team Edition**에 Aelix를 연결하는 외부 봇 Gateway와 Aelix 확장 패키지입니다.
공식 Agents 플러그인이나 유료 라이선스 없이 일반 Bot Account와 REST/WebSocket으로 연결합니다.
**0.1.0 alpha**이며 로컬 서버·RPC 자식 프로세스로 테스트했습니다. 실제 사내 서버·Aelix·모델 연결은
배포 환경에서 검증해야 합니다.

## 설치

Python 3.11 이상과 동작하는 Aelix CLI를 준비합니다. Gateway 서비스 계정의 Aelix 모델 설정도 준비하세요.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
cp config.example.toml config.toml
```

설정에서 `mattermost.url`, 허용할 사람의 `allowed_users` **User ID**, 필요하면
`allowed_channels` 채널 ID와 `aelix.command` 실제 실행 파일 경로를 변경합니다.
토큰은 설정 파일에 저장하지 않고 `MATTERMOST_TOKEN` 환경변수로 전달합니다.

```bash
read -r -s -p 'Mattermost bot token: ' MATTERMOST_TOKEN
export MATTERMOST_TOKEN
aelix-mattermost check-config --config config.toml
aelix-mattermost doctor --config config.toml --check-aelix
aelix-mattermost run --config config.toml
```

Mattermost 관리자가 Bot Account 생성을 켜고 `aelix` 봇을 **Member** 권한으로 만듭니다.
사용할 팀·채널에 봇을 추가하고, 그룹 DM은 봇을 참여자로 포함해서 만듭니다.
사내 CA는 `ca_file`로 설정합니다. HTTP는 로컬 검증 시에만 명시적으로 허용하세요.

## 사용

| 상황 | 입력 |
| --- | --- |
| 개인 DM | `이 오류의 원인을 설명해주세요` |
| 그룹 DM·공개·비공개 채널 | `@aelix 이 오류의 원인을 설명해주세요` |
| 후속 질문 | 같은 스레드에서 `@aelix 추가 질문` |
| 사용법 | `@aelix !help` |
| 현재 내 요청 취소 | `@aelix !cancel` |
| 현재 대화 컨텍스트 초기화 | `@aelix !reset` |

개인 DM에서는 명령에도 멘션이 필요 없습니다. 네이티브 `/aelix` Slash Command 방식은 아닙니다.
실행 상태 메시지가 완료 후 스레드 답변으로 바뀝니다. 첨부파일 자동 분석과 토큰 단위 스트리밍은
이 버전에 포함되지 않습니다.

## 그룹 세션과 분석 도구

기본 `session_scope = "user"`는 같은 스레드에서도 사용자별로 모델 대화를 분리합니다.
공동 분석에는 `"thread"`를 사용합니다. 같은 세션은 순서대로 실행하며, 공유 스레드에서도
현재 실행을 취소할 수 있는 사람은 그 요청자입니다.

처음에는 `allowed_tools = []`로 도구 없는 채팅을 검증하세요. 이후 검토한 도구만 허용합니다.

```toml
[aelix]
allowed_tools = ["equipment_summary"]
extensions = ["./extensions/equipment.py"]
```

Gateway는 도구 이름과 호출 예산을 검사하는 정책 확장이 정상 등록되기 전에는 프롬프트를 보내지 않습니다.
도구 이름 허용은 샌드박스가 아닙니다. 도구 자체가 읽기 전용 동작과 데이터 권한을 보장해야 합니다.
도구에서 `aelix_mattermost.context.request_context()`의 `user_id`를 읽어 내부 시스템 권한을 검증하세요.
Mattermost 토큰은 Aelix 자식 프로세스 환경에서 제거합니다.

## 운영

- `!reset`은 모델 컨텍스트를 새로 시작합니다. 과거 파일 삭제 명령이 아닙니다.
- Aelix 세션에는 프롬프트와 도구 결과가 남으므로 보관·삭제 정책이 필요합니다.
- 이미 접수한 Post ID는 중복 실행하지 않습니다. 연결이 끊긴 동안의 메시지 자동 복구는 없습니다.
- 중단 전에 접수한 요청은 도구 중복 실행을 피하기 위해 자동 재실행하지 않습니다.
- 같은 `state_dir`에는 서비스 한 개만 실행할 수 있습니다.
- 권한이 좁은 전용 서비스 계정으로 운영하세요.

[systemd 예제](deploy/aelix-mattermost.service)와 [배포 절차](docs/deployment.md)를 참고하세요.
Gateway와 Aelix 확장으로 설치하는 패키지이며 Mattermost Plugin Management에 업로드하는 서버 플러그인이 아닙니다.
