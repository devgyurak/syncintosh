# syncintosh (한국어)

> sync + Macintosh — 집 맥북과 회사 맥북의 개발 환경을 맞춥니다.

`syncintosh` 는 개발 환경 설정(셸·IDE·Docker 등)을 **종단간 암호화된 git 저장소**를 통해
여러 대의 맥 사이에서 맞추는 CLI 도구입니다. 무엇이 다른지 보여주고, 무엇을 맞출지 고르게
하며, 충돌은 **포맷 인식 3-way merge** 와 선택적 **LLM 보조**로 해결합니다.

서버는 암호문만 봅니다: 파일 **내용**, **경로**, **디렉터리 구조**가 모두 암호화되고,
저장소는 직접 관리하는 아무 git remote(홈서버, Gitea, 비공개 GitHub 리포)에 둘 수 있습니다.

## 상태

초기 골격입니다. 현재 구현된 부분은 충돌 해결기가 딛고 설 순수한 키 단위 merge 엔진입니다.
구조는 `AGENTS.md`, 에이전트 관련 안내는 `CLAUDE.md` 를 보세요.

## 개발

```bash
uv sync --all-extras                            # 환경 구성
uv run ruff format && uv run ruff check --fix   # 포맷 + 린트
uv run ty check                                 # 타입 검사
uv run pytest                                   # unit + integration, 커버리지 90% 하한
```

작업을 끝내기 전에 네 게이트를 모두 통과시키세요 — `AGENTS.md` §1 참고.

## 라이선스

AGPL-3.0-or-later. `LICENSE` 참고.
