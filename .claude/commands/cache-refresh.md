# 캐시 수동 갱신

Jira·Confluence·Drive 캐시를 강제 갱신한다. 서버 재시작 없이 최신 데이터를 반영할 때 사용.

## 실행 순서

1. 현재 캐시 상태 확인
2. 갱신 트리거
3. 완료 후 상태 재확인

## 커맨드

```bash
# 현재 상태 확인
curl -s http://localhost:8000/api/status | python3 -m json.tool

# 갱신 트리거 (백그라운드에서 수행됨, 즉시 응답)
curl -s -X POST http://localhost:8000/api/refresh

# 30초 후 완료 확인
sleep 30 && curl -s http://localhost:8000/api/status | python3 -m json.tool
```

## 참고

- 갱신은 백그라운드에서 비동기 처리됨 — 즉시 완료 아님
- Jira 6000+건 기준 약 30~60초 소요
- `loading: true` 상태에서 검색 가능 (기존 캐시 유지)
- 1시간마다 자동 갱신되므로 평소엔 수동 갱신 불필요
