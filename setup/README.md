# QA Hub 로컬 자동 시작 설정

## 1. launchd 에이전트 설치

```bash
cp setup/com.local.qa-search.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local.qa-search.plist
```

재설치 / 맥 초기화 후에도 위 명령어로 복구 가능.

## 2. 주의사항

- `gws` 경로: `/Users/kimhyewon/.local/node-current/bin/gws`
  → plist 의 `EnvironmentVariables.PATH` 에 포함되어 있어야 함
  → gws 재설치 후 경로가 바뀌면 plist 수정 필요

- 로그 위치: `~/qa-search/logs/server.log`

## 3. 수동 제어

```bash
# 중지
launchctl unload ~/Library/LaunchAgents/com.local.qa-search.plist

# 시작
launchctl load ~/Library/LaunchAgents/com.local.qa-search.plist

# 상태 확인
curl http://localhost:8000/api/status
```
