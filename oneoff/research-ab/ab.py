"""조사 단계 모델 비교 (일회성). 같은 날, 같은 선수, 같은 검색 한도로 Sonnet과 Opus 조사 노트를 나란히 만든다."""
import json, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import anthropic
import league as L

d = datetime.now(ZoneInfo("Asia/Seoul")).date()
client = anthropic.Anthropic(max_retries=8)
out = []
p = ROOT / "oneoff" / "research-ab" / "out" / f"ab_{d:%Y%m%d}.json"
p.parent.mkdir(parents=True, exist_ok=True)
for pid in ["news", "semi", "macro", "value"]:
    for model in ["claude-sonnet-5-5", "claude-opus-5-5"]:
        usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0, "by_month": {}}
        t0 = time.time()
        try:
            note = L.research(client, usage, d, model, pid)
        except Exception as e:
            note = f"오류: {e}"
        rec = {"pid": pid, "model": model, "sec": round(time.time() - t0), "usage": usage, "note": note}
        out.append(rec)
        print(pid, model, rec["sec"], "s", round(usage["cost_usd"], 3), "$", len(note), "chars", flush=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1))  # 한 건마다 저장
