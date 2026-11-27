"""11/13 삼성전자 시세가 빠진 날을 바로잡으려고 그 주 시세만 받아 둔다 (일회성)."""
import json
from pathlib import Path
import yfinance as yf

out = Path(__file__).parent / "out"
out.mkdir(exist_ok=True)
df = yf.download("005930.KS", start="2026-11-10", end="2026-11-18", interval="1d",
                 auto_adjust=False, progress=False, threads=False)
if hasattr(df.columns, "levels"):
    df.columns = df.columns.get_level_values(0)
rows = {str(i.date()): {"Open": float(r["Open"]), "Close": float(r["Close"])} for i, r in df.iterrows()}
print(rows)
(out / "005930_2026-11.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
