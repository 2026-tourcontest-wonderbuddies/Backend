# scripts

## build_period_places.py

홈 화면 `JEJU BY TIME OF DAY` 카드에 들어가는 시간대별 장소 목록을 만든다.
출력물 `data/period_places.json` 을 `GET /api/places/by-period/` 가 런타임에 읽는다.

**배포 시 실행되지 않는다.** 산출물 JSON 만 커밋해서 쓴다.

### 근거가 두 갈래다

| 시간대 | 근거 | `evidence` |
|---|---|---|
| 아침 · 낮 · 노을 · 밤 | AI Hub 실측 도착시각의 시간대 쏠림(lift) | `arrival` |
| 새벽 | 영업 · 개방 시간 | `hours` |

새벽만 다른 이유: AI Hub 매칭 방문 9,851건 중 새벽 도착이 75건(0.8%)뿐이라
도착시각으로는 순위가 서지 않는다(부트스트랩 200회 상위10 유지율 60%, 최저 30%).
그래서 `data/hours_cache.json` 에서 04:00~07:00 사이에 **문을 여는** 곳을 뽑고
AI Hub 방문 기록이 있는 곳으로 좁혔다. `open` 시각이 그 구간 안에 있을 것을
요구하면 00:00/01:00부터 열려 있는 24시간 편의점이 자연히 빠진다.

### 입력

`data/` 안의 두 파일은 레포에 있다.

- `data/hours_cache.json` — 새벽 판정용
- `data/jeju_places_stay_time.csv`, `data/jeju_restaurants_stay_time.csv` — 자체 점검용

나머지 둘은 **레포 밖 AI Hub 원본 데이터**라 각자 경로를 넘겨야 한다.

- `03_visit_제주.csv` — AI Hub 국내 여행로그 H권역(제주) 방문 로그
- `place_link_verified.csv` — AI Hub `PLACE_KEY` ↔ TourAPI `content_id` 매칭 결과

```bash
python scripts/build_period_places.py \
  --ai-hub /path/to/AI_Hub/03_visit_제주.csv \
  --link   /path/to/matching/data/output/place_link_verified.csv
```

기본값은 작성자 로컬 경로다. 인자를 주지 않으면 그 경로에서 찾는다.

### 자체 점검

스크립트 끝의 `check()` 가 실패하면 파일을 쓰지 않는다.

- 5개 시간대 각 10곳
- 모든 `content_id` 가 `data/jeju_*_stay_time.csv` 에 존재
- `dawn` 은 전부 `evidence=="hours"`, 나머지는 전부 `"arrival"`
- `arrival` 목록은 `score` 내림차순
- 카드 간 중복 4건 이하

### 데이터 한계

- 수집 기간 2023-04~09. **겨울 없음** — 해수욕장이 상위에 몰리는 원인
- 방문 로그 30,954행 중 `content_id` 귀속은 9,851행(31.8%). 숙소 4,483건이
  매칭 대상이 아니어서 빠지고, 그 결과 **매칭 데이터의 밤 비중이 12.4%**로
  전체(24.3%)의 절반이다. 밤 목록은 "숙소 아닌 밤 활동" 기준으로 읽어야 한다
- `ARRIVE_DT` 가 `VISIT_ORDER` 와 순서가 어긋나는 여행이 1,488건 중 603건.
  개별 행 시각엔 노이즈가 있고 집계에서 상쇄된다는 가정으로 쓴다
- 낮은 lift 최대가 1.61로 시간대 색이 거의 없다(낮이 제주 방문의 기본값).
  정렬이 통계적 근거라기보다 "온종일 가는 곳 걷어내기"에 가깝다
