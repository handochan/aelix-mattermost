# aelix-mattermost

Mattermost **Team Edition**에 Aelix를 연결하는 외부 봇 Gateway와 Aelix 확장 패키지입니다.
공식 Agents 플러그인이나 유료 라이선스 없이 일반 Bot Account와 REST/WebSocket으로 연결합니다.
**0.3.0 alpha**이며 Mattermost 서버와 Aelix RPC 동작을 따르는 로컬 테스트 대역으로 검증했고,
0.3.0 기능은 실제 Aelix RPC(스크립트 모델)로도 확인했습니다.
실제 사내 서버·Aelix·모델 연결은 배포 환경에서 `doctor`와 첫 메시지로 확인해야 합니다.

## 설치

Python 3.11 이상과 동작하는 Aelix CLI(0.1.0b2 기준)를 준비합니다. Gateway 서비스 계정의 Aelix
모델 설정(`models.json`)도 준비하세요.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
cp config.example.toml config.toml
```

설정에서 `mattermost.url`, 허용할 사람의 `allowed_users` **User ID**, 필요하면
`allowed_channels` 채널 ID와 `aelix.command` 실제 실행 파일 경로를 변경합니다.
토큰은 설정 파일에 저장하지 않고 `MATTERMOST_TOKEN` 환경변수나 `token_file`(Docker·Kubernetes
Secret 파일 등, 환경변수보다 우선)로 전달합니다.

```bash
read -r -s -p 'Mattermost bot token: ' MATTERMOST_TOKEN
export MATTERMOST_TOKEN
aelix-mattermost check-config --config config.toml
aelix-mattermost doctor --config config.toml --check-aelix
aelix-mattermost run --config config.toml
```

`check-config`는 네트워크에 연결하지 않습니다. `doctor`는 봇 계정과 WebSocket 인증(`hello` 수신)을
확인하고, `--check-aelix`는 모델 요청 없이 Aelix RPC 기동, Aelix 설정 디렉터리와 모델 확인
(`models.json`이나 Aelix 카탈로그에 없는 모델 ID는 실패), 세션·정책 준비 상태를 점검합니다.
모델 인증 정보와 엔드포인트 연결은 모델 요청이 필요하므로 첫 메시지로 확인하세요.
봇에 System Console 역할이 있으면 경고합니다.
Mattermost 관리자가 Bot Account 생성을 켜고 `aelix` 봇을 **Member** 권한으로 만듭니다.
사용할 팀·채널에 봇을 추가하고, 그룹 DM은 봇을 참여자로 포함해서 만듭니다.
사내 CA는 `ca_file`로 설정합니다. HTTP는 로컬 검증 시에만 명시적으로 허용하세요.

## 사용

| 상황 | 입력 |
| --- | --- |
| 개인 DM | `이 오류의 원인을 설명해주세요` (파일 첨부 가능) |
| 그룹 DM·공개·비공개 채널 | `@aelix 이 오류의 원인을 설명해주세요` |
| 후속 질문 | 같은 스레드에서 `@aelix 추가 질문` |
| 실행 중에 지시 추가 | 그냥 다음 메시지를 보내거나 `!steer 테스트부터 확인해줘` |
| 상태·사용량 | `!status`, `!usage` |
| 실행 중·대기 중인 내 요청 중단 | `!stop` (`!cancel`) |
| 대화 컨텍스트 초기화 | `!new` (`!reset`) |
| 모델 변경·도구 확인·압축 | `!model`, `!tools`, `!compact` |
| 사용법 | `!help` |

채널에서는 명령에도 `@aelix`를 붙이고, 개인 DM에서는 멘션이 필요 없습니다. Mattermost 클라이언트는
맨 앞의 `/`를 자체 Slash Command로 가로채므로 `!new`, 앞에 공백을 넣은 ` /new`, `@aelix /new`로
입력하거나, 선택 기능인 실제 [`/aelix` Slash Command](docs/slash-command.md)를 등록해 쓰세요.
멘션은 Mattermost 서버 규칙을 따릅니다. `@aelix님`, `@aelix에게`처럼 글자를 붙이거나 코드 안에
쓴 멘션은 인식하지 않으니 `@aelix` 뒤에 공백을 두세요.

요청 실행이 시작되면 스레드에 `응답을 준비하고 있습니다…`를 올리고, 답변은 새 스레드 답글로 게시하므로
일반 답글처럼 알림이 갑니다. 그 뒤 준비 메시지는 삭제합니다. 실패·시간 초과·취소 안내도 같은
방식입니다. 긴 답변은 코드 블록을 깨뜨리지 않고 나눕니다.

## 대화 기능 (0.3.0)

자세한 내용은 [대화 기능 문서](docs/features.md)(영문)를 참고하세요.

- **실행 중 끼어들기** (`busy_mode`): 내 요청이 실행 중일 때 보낸 메시지는 기본(`steer`)으로 진행 중인
  작업에 끼워 넣고 👀 반응을 답니다. 앞 답변이 이미 끝났으면 각 메시지가 자기 스레드에 답을 받습니다.
  `interrupt`는 진행 중 작업을 멈추고 새 메시지를, `queue`는 차례로 처리합니다. 공유 스레드에서 다른
  사람의 메시지는 항상 대기합니다.
- **진행 상황**: 준비 메시지가 `progress_interval`초마다 단계(생각·작성·도구 이름·재시도·압축),
  도구 호출 수, 경과 시간으로 바뀌고 입력 중 표시가 나옵니다. `progress = "stream"`은 작성 중인
  답변 끝부분도 보여줍니다. 수정은 알림을 보내지 않습니다.
- **스레드 이전 대화**: 채널 스레드에서 처음 불리면 그 전의 스레드 글을, 이후에는 새로 올라온 글을
  "신뢰할 수 없는 인용"으로 프롬프트 앞에 붙입니다(`thread_history_posts`, 기본 30개). 봇 사용이
  허용된 사람과 봇의 글만 인용하고, 다른 멤버·웹훅·연동·다른 봇의 글은 넣지 않습니다.
- **첨부파일**: 텍스트 파일은 본문에 넣고, 이미지는 이미지 입력을 지원하는 모델에 이미지로 전달합니다.
  도구가 허용된 대화에서는 파일을 작업 폴더 `attachments/`에도 저장해 도구로 열 수 있게 합니다
  (대화당 200MB, `!new`로 삭제). 파일만 보내도 됩니다. 모델이 도구로 `outbox/`에 쓴 파일은 답변에 첨부합니다.
- **Mattermost 시스템 프롬프트**: Aelix에 DM/채널 상황, Mattermost 서식, 명령, 첨부파일과 outbox,
  허용 도구를 알려줍니다. `aelix.system_prompt`와 채널별 `prompt`가 뒤에 붙습니다.
- **채널별 설정** `[channels."채널 ID"]`: `prompt`, `require_mention = false`(멘션 없이 응답하는 채널),
  `allowed_tools`(그 채널의 도구 목록).
- **모델 선택**: `aelix.models`에 적은 모델 중에서 `!model`로 대화별로 고릅니다.
- **페어링**: `pairing = true`이면 허용되지 않은 사용자가 봇에 DM을 보낼 때 승인 코드를 받고,
  관리자(`admins`)가 봇 DM에서 `!pair approve 코드` 또는 `aelix-mattermost pairing approve 코드`로
  승인합니다. 거절된 사용자는 24시간 동안 새 코드를 받지 않습니다.

## 출력 안전과 연동 게시물

봇의 모든 게시·수정에 `unsafe_links`를 설정해 서버가 링크 미리보기나 이미지를 가져오지 않습니다.
클라이언트는 답변의 Markdown 이미지를 불러올 수 있으므로 답변 속 링크·이미지는 신뢰하지 마세요.
모델 답변의 `@channel`, `@all`, `@here`, `@사용자`, 그룹 멘션에는 코드 밖에서 보이지 않는
문자(U+2060)를 넣어 이 멘션으로는 알림이 가지 않습니다. 이 처리가 코드나 링크를 바꾸는 드문
Markdown과, 테스트했지만 증명하지는 못한 Mattermost 파서 이식의 한계는 [SECURITY.md](SECURITY.md)에
정리했습니다. 다만 사용자가 직접 설정한 알림 키워드와 이름(first name) 멘션은 `@` 없이 일치하므로,
답변에 그 단어가 있으면 해당 사용자에게 알림이 갈 수 있습니다. Incoming Webhook, 사용자 정의
Slash Command 응답, OAuth 앱, 플러그인 게시물(`from_webhook`, `from_oauth_app`, `from_plugin`)은
허용 사용자 ID가 있어도 무시합니다. 플러그인 API를 통해 호출자 계정으로 게시하는 플러그인 Slash
Command는 구분할 수 없습니다.

## 그룹 세션과 분석 도구

기본 `session_scope = "user"`는 같은 스레드에서도 사용자별로 모델 대화를 분리합니다.
공동 분석에는 `"thread"`를 사용합니다. 이때 참여자 모두가 이전 요청자의 도구 원본 결과까지 담긴
모델 컨텍스트를 공유하므로, 모든 참여자가 봐도 되는 데이터에만 사용하세요. 같은 세션은 순서대로
실행하며, 현재 실행을 취소할 수 있는 사람은 그 요청자입니다.

처음에는 `allowed_tools = []`로 도구 없는 채팅을 검증하세요. 이후 검토한 도구만 허용합니다.
`aelix-mattermost tools --config config.toml`이 허용할 수 있는 내장 도구 이름과 설치된 확장 패키지를
보여주고, `doctor --check-aelix`가 설정한 도구 목록마다 Aelix를 띄워 모르는 이름을 미리 잡아냅니다.
확장은 `.py` 파일·디렉터리 경로나, `aelix extension install`로 Aelix 환경에 설치한 패키지의 모듈
(`"my_pkg"`, `"my_pkg.tools:setup"`)로 지정합니다. Docker는 [확장 설치](docs/docker.md#extensions)를
참고하세요.

```toml
[aelix]
allowed_tools = ["equipment_summary"]
extensions = ["./extensions/equipment.py"]
```

Gateway는 도구 이름과 메시지당 호출 예산(`max_tool_calls`, Aelix 자동 재시도 포함)을 검사하는
정책 확장이 정상 등록되기 전에는 프롬프트를 보내지 않습니다. RPC 모드에는 승인 창이 없어 이
정책이 유일한 허용 목록입니다. Aelix 내장 도구(`bash`, `edit`, `find`, `grep`, `ls`, `read`,
`write`, `aelix_status`)도 `allowed_tools`에 적으면 활성화됩니다. MCP 서버는 `aelix.mcp_config`로
지정한 파일만 사용하고, 서비스 계정의 `mcp.json`은 무시합니다.
도구 이름 허용은 샌드박스가 아닙니다. 도구 자체가 읽기 전용 동작과 데이터 권한을 보장해야 합니다.
도구에서 `aelix_mattermost.context.request_context()`의 `user_id`를 읽어 내부 시스템 권한을 검증하세요.
이 import는 Aelix와 같은 Python 환경에 aelix-mattermost가 설치된 경우에만 동작하며, 그렇지 않으면
`AELIX_MATTERMOST_CONTEXT_FILE`이 가리키는 JSON 파일을 읽으세요. 확장 코드는 RPC 통신에 쓰이는
stdout에 출력하면 안 되며, 로그는 stderr로 남기세요.
Mattermost 토큰은 Aelix 자식 프로세스 환경에서 제거하지만, 같은 계정으로 실행되는 shell·read
도구는 `token_file`이나 Gateway 프로세스 환경을 읽을 수 있습니다.

`/mattermost` 도움말 확장은 빌드한 wheel을 `aelix extension install`로 설치합니다. 설치할 때
`aiohttp`를 패키지 인덱스에서 받으므로 폐쇄망에서는 먼저 Aelix 환경에 설치하세요. 다시 빌드한
wheel을 같은 경로에서 재설치하려면 `--repin`이 필요합니다.

## 운영

- `!new`(`!reset`)는 모델 컨텍스트를 새로 시작합니다. 과거 파일 삭제 명령이 아닙니다.
- 첨부파일과 outbox 파일은 대화 작업 폴더(`work_dir`)에 남으므로 보관·삭제 정책에 포함하세요.
- Aelix 세션에는 프롬프트와 도구 결과가 남으므로 보관·삭제 정책이 필요합니다.
- 이미 접수한 Post ID는 중복 실행하지 않습니다. 몇 분 안에 같은 서버 노드로 WebSocket이 다시
  연결되면 서버가 놓친 이벤트(최대 128개)를 다시 보내 주지만, 과거 메시지 자동 복구는 없습니다.
- 재시작·종료로 중단된 요청은 준비 메시지에 안내를 표시하고, 도구 중복 실행을 피하기 위해 자동으로
  다시 실행하지 않습니다. 앞 요청을 기다리던 요청은 준비 메시지가 없어 안내 없이 종료됩니다.
- Aelix 자식 프로세스는 `max_live_processes`(기본 8개, 개당 약 85 MiB)까지 유지합니다. 한도에
  도달하면 새로 시작하기 전에 가장 오래 사용하지 않은 유휴 프로세스를 종료합니다. 실행 슬롯을 기다리는
  요청의 프로세스도 유휴로 보며, 그 요청은 차례가 오면 대화 기록을 이어 새 프로세스에서 실행합니다.
  실행 중이거나 컨텍스트를 압축(compaction) 중인 프로세스는 종료하지 않습니다.
- `run`은 10초마다 `<state_dir>/health.json`을 갱신합니다. `aelix-mattermost healthcheck --config
  config.toml`은 토큰과 네트워크 없이 이 파일만 읽어, 최신이고 WebSocket이 연결된 경우에만 0으로
  종료합니다. 상태 디렉터리는 비공개이므로 서비스 계정으로 실행하세요.
- 같은 `state_dir`에는 서비스 한 개만 실행할 수 있습니다. 0.3.0은 처음 시작할 때 `gateway.db`를
  스키마 2로 올리며, 0.2.0은 이 DB로 시작하지 않습니다(0.1.0은 첫 요청에서 SQLite 오류로 종료).
  업그레이드 전에 서비스를 멈추고 상태 디렉터리를 백업해 두었다가 이전 버전으로 되돌릴 때 복원하세요.
  0.1.0에서 올릴 때 필요한 조치는 [CHANGELOG.md](CHANGELOG.md#upgrading-from-010)를 참고하세요.
- 권한이 좁은 전용 서비스 계정이나 Docker 이미지로 운영하세요.

[배포 절차](docs/deployment.md), [Docker 배포](docs/docker.md), [보안 경계](SECURITY.md)를
참고하세요. [systemd 예제](deploy/aelix-mattermost.service)는 `HOME=/var/lib/aelix-mattermost/home`,
`AELIX_CODING_AGENT_DIR=/var/lib/aelix-mattermost/aelix-agent`를 사용하므로 `models.json`은
`/var/lib/aelix-mattermost/aelix-agent/`에 둡니다.
Gateway와 Aelix 확장으로 설치하는 패키지이며 Mattermost Plugin Management에 업로드하는 서버 플러그인이 아닙니다.
