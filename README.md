# AI 투자 리그

Claude 열 명(가치투자자, 추세 추종자, 뉴스 헌터, 역발상, 반도체 전문, 바이오 전문, 배당 방어형, 수급 추종, 거시경제, 차트)과 랜덤 원숭이가 가상 1천만 원으로
한국 대형주 20개와 미국 대형주 15개를 한 계좌(원화)로 모의 매매합니다. KOSPI200(KODEX 200)과 S&P500(SPY, 원화 환산)이 비교 기준이에요.
실제 돈은 들어가지 않고, 결과는 구경용이에요.

## 진행 방식
- 평일 16:40(KST)에 GitHub Actions가 자동 실행
- 한국 종목은 다음 날 한국장 시가, 미국 종목은 주문 낸 날 밤 미국장 시가에 체결(원달러 환율 반영)하고, 오늘 종가로 평가한 뒤, 각 선수가 내일 주문을 냄
- 매도 시 수수료와 거래세로 약 0.2% 비용 반영
- 휴장일은 자동으로 건너뜀
- 결과: `docs/index.html` (GitHub Pages로 보기), 슬랙 알림(선택)

## 처음 설정 (10분)
1. GitHub에서 새 저장소를 만들고 이 폴더 내용을 그대로 올리기
2. 저장소 **Settings > Secrets and variables > Actions**
   - Secrets에 `ANTHROPIC_API_KEY` 추가 (Claude Console에서 발급한 키)
   - (선택) Secrets에 `SLACK_WEBHOOK_URL` 추가: 슬랙 Incoming Webhook 주소
3. **Settings > Pages**: Source를 `Deploy from a branch`, 브랜치 `main`, 폴더 `/docs`로
   - 페이지 주소가 나오면 Variables에 `LEAGUE_PAGE_URL`로 넣기 (슬랙 알림에 링크가 붙음)
   - 무료 계정은 공개 저장소에서만 Pages가 돼요. 키는 Secrets에만 있으니 공개해도 괜찮아요
4. **Actions** 탭에서 워크플로를 켜고 `Run workflow`로 한 번 수동 실행
   - 첫날은 주문만 내고, 다음 거래일부터 체결이 시작돼요

## 비용 (한 달 약 100달러에 맞춤)
- 조사: Claude Opus 5.5가 선수마다 웹 검색 최대 5회 (`LEAGUE_RESEARCH_MODEL`, `LEAGUE_SEARCH_MAX`)
- 판단: Claude Fable 5.1 (`LEAGUE_MODEL`)
- 예상: 거래일 하루 4~5달러, 한 달 약 90~100달러
- 장 마감(15:50) 전에 실행되면 아무것도 하지 않고 끝나요
- **크레딧 구간별 운영** (`CREDIT_WINDOWS`)
  - 10/16: 10/18 만료 크레딧용 최대 강도 (조사 Fable 5.1, 검색 10회)
  - 10/19~11/17: 11/18 만료 크레딧 100달러를 나눠 씀 (조사 Sonnet 5.5, 검색 6회)
  - 남은 예산을 남은 거래일로 나눈 하루 몫에 따라 판단 모델이 Fable 5.1, Opus 5.5, Sonnet 5.5 순으로 자동 조정
  - 11/18 이후: 월 100달러 기본 모드(매달 1일~말일). 크레딧이 없으면 호출이 실패하고 보유만 유지, 크레딧이 생기면 자동으로 다시 판단
- **안전장치**: 이번 달 사용액이 `LEAGUE_MONTHLY_BUDGET`(기본 100달러)에 가까워지면
  Sonnet으로 내리고 조사를 생략, 다 쓰면 그달 남은 날은 보유만 유지
- 페이지 맨 아래에 이번 달 사용액이 표시돼요. 정확한 금액은 Claude Console의 Usage에서 확인

## 손볼 곳
- 종목: `league.py`의 `UNIVERSE`
- 선수 성격: `PLAYERS`의 `persona` (새 선수를 추가해도 돼요)
- 처음부터 다시: `python league.py --reset --force`
- API 없이 미리보기: `python league.py --dry-run` → `docs/dry_run.html`
