"""셀프 DM 서재의 빈 링크 제목 채우기 (일회성 작업, GitHub Actions에서 실행).

1) 페이지를 직접 받아 <title>, og:title, 설명, 본문 앞부분을 뽑는다.
   cell.com은 Crossref(PII), PubMed/PMC는 NCBI E-utilities로 메타데이터를 받는다.
2) Claude가 그걸 보고 제목과 한국어 한 줄 설명을 정리한다.
   직접 받은 정보가 없으면 웹 검색(최대 3회)으로 찾는다.
3) 결과: out/s99titlesYYYYMMDD.json (url → {title, desc}), out/report.json
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import anthropic
import requests

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("TITLES_MODEL", "claude-fable-5-1")
WORKERS = int(os.environ.get("TITLES_WORKERS", "4"))
TODAY = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
OUT = HERE / "out" / f"s99titles{TODAY}.json"
REPORT = HERE / "out" / "report.json"

# 로그인이 필요하거나 만료된 파일 링크라 제목을 알 수 없는 곳
SKIP = ("blackboard.unist.ac.kr", "drive.google.com", "docs.google.com", "colab.research.google.com",
        "s3-us-west-2.amazonaws.com", "files.slack.com")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"})

client = anthropic.Anthropic(max_retries=8)


def meta(page: str, name: str) -> str:
    for pat in (rf'<meta[^>]+(?:property|name)=["\']{name}["\'][^>]+content=["\']([^"\']+)',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{name}["\']'):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def page_info(url: str) -> dict:
    info = {"title": "", "og": "", "desc": "", "text": "", "src": ""}
    h = urlparse(url).netloc
    try:
        # cell.com: Crossref에서 PII로 조회
        m = re.search(r"(S\d{4}-\d{3}[\dX]\(\d{2}\)\d{5}-\d)", url)
        if m and "cell.com" in h:
            pii = m.group(1)
            for key in (pii, re.sub(r"[^0-9A-Z]", "", pii)):
                r = S.get("https://api.crossref.org/works",
                          params={"filter": f"alternative-id:{key}", "rows": 1}, timeout=20)
                items = r.json().get("message", {}).get("items", []) if r.ok else []
                if items:
                    it = items[0]
                    info["title"] = (it.get("title") or [""])[0]
                    info["desc"] = re.sub("<[^>]+>", "", it.get("abstract", ""))[:1500]
                    info["og"] = (it.get("container-title") or [""])[0]
                    info["src"] = "crossref"
                    return info
        # PubMed, PMC: NCBI E-utilities
        m = re.search(r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|ncbi\.nlm\.nih\.gov/pubmed/)(\d+)", url)
        pm = re.search(r"(PMC\d+)", url)
        if m or pm:
            db, uid = ("pubmed", m.group(1)) if m else ("pmc", pm.group(1).replace("PMC", ""))
            r = S.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                      params={"db": db, "id": uid, "retmode": "json"}, timeout=20)
            res = r.json().get("result", {}).get(uid, {}) if r.ok else {}
            if res.get("title"):
                info.update(title=res["title"], og=res.get("fulljournalname", res.get("source", "")), src="ncbi")
                return info
        # 네이버 블로그는 본문이 iframe 안에 있어서 모바일 주소가 낫다
        u = url.replace("://blog.naver.com", "://m.blog.naver.com")
        r = S.get(u, timeout=20, allow_redirects=True)
        if not r.ok:
            info["src"] = f"http {r.status_code}"
            return info
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype:
            info["src"] = f"not html ({ctype.split(';')[0]})"
            info["title"] = Path(urlparse(r.url).path).name
            return info
        r.encoding = r.apparent_encoding or r.encoding
        page = r.text
        t = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        info["title"] = html.unescape(t.group(1)).strip() if t else ""
        info["og"] = meta(page, "og:title") or meta(page, "citation_title")
        info["desc"] = meta(page, "og:description") or meta(page, "description")
        body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
        info["text"] = re.sub(r"\s+", " ", body)[:2500]
        info["src"] = "fetch"
    except Exception as e:  # 네트워크 오류는 Claude 검색으로 넘김
        info["src"] = f"error {type(e).__name__}"
    return info


PROMPT = """아래 링크는 한 대학원생이 슬랙 셀프 DM에 저장해 둔 것이다. 이 링크의 제목과 한국어 설명 한 줄을 만들어라.

URL: {url}
직접 받은 정보 ({src}):
- <title>: {title}
- og/저널: {og}
- 설명: {desc}
- 본문 앞부분: {text}

규칙
- title: 페이지의 실제 제목. 논문이면 논문 제목 그대로(영어면 영어로). 사이트 이름 꼬리("| Nature" 등)는 뺀다.
- desc: 무엇에 관한 링크인지 한국어 한 문장(60자 안팎). 논문이면 핵심 발견, 도구면 하는 일. "~한 연구", "~하는 도구"처럼 끝맺는다. 가운뎃점(·)으로 단어를 잇지 않는다.
- 직접 받은 정보가 비었거나 로그인 화면, 오류 페이지, 봇 차단 페이지 같으면 웹 검색으로 이 URL이 가리키는 문서를 찾아라.
- 끝내 확인 못 하면 title을 빈 문자열로 둔다. 추측으로 지어내지 않는다.

마지막 줄에 JSON 한 줄만 출력: {{"title": "...", "desc": "..."}}"""


def ask_claude(url: str, info: dict) -> dict:
    need_search = not (info["title"] or info["og"]) or info["src"].startswith(("http", "error"))
    kw = {}
    if need_search or info["src"] == "fetch" and len(info["text"]) < 200:
        kw["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    msgs = [{"role": "user", "content": PROMPT.format(url=url, **{k: (v or "(없음)")[:2500] for k, v in info.items()})}]
    usage = {"in": 0, "out": 0, "search": 0}
    resp = None
    for _ in range(3):
        resp = client.messages.create(model=MODEL, max_tokens=3000, messages=msgs, **kw)
        usage["in"] += resp.usage.input_tokens
        usage["out"] += resp.usage.output_tokens
        stu = getattr(resp.usage, "server_tool_use", None)
        usage["search"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        if resp.stop_reason != "pause_turn":
            break
        msgs = msgs + [{"role": "assistant", "content": resp.content}]
    text = "".join(b.text for b in resp.content if b.type == "text")
    found = re.findall(r"\{[^{}]*\"title\"[^{}]*\}", text)
    out = {"title": "", "desc": ""}
    if found:
        try:
            out = json.loads(found[-1])
        except json.JSONDecodeError:
            pass
    return {"title": (out.get("title") or "").strip(), "desc": (out.get("desc") or "").strip(), "usage": usage}


def work(item):
    url = item["u"]
    h = urlparse(url).netloc
    if any(s in h for s in SKIP):
        return url, {"title": "", "desc": "", "note": "skip"}
    info = page_info(url)
    try:
        res = ask_claude(url, info)
    except Exception as e:
        return url, {"title": "", "desc": "", "note": f"claude error {e}"[:200], "src": info["src"]}
    res["src"] = info["src"]
    return url, res


def main():
    items = json.loads((HERE / "urls.json").read_text())
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(items)
    items = items[:limit]
    results, t0 = {}, time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(work, it) for it in items]
        for i, f in enumerate(as_completed(futs), 1):
            url, res = f.result()
            results[url] = res
            print(f"[{i}/{len(items)}] {'OK ' if res.get('title') else '-- '} {url[:90]} | {res.get('title', '')[:60]}",
                  flush=True)
    titles = {u: {"title": r["title"], "desc": r["desc"]} for u, r in results.items() if r.get("title")}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(titles, ensure_ascii=False, indent=1))
    tot = {"in": 0, "out": 0, "search": 0}
    for r in results.values():
        for k in tot:
            tot[k] += r.get("usage", {}).get(k, 0)
    cost = tot["in"] / 1e6 * 10 + tot["out"] / 1e6 * 50 + tot["search"] * 0.01  # Fable 5.1 기준 추정
    REPORT.write_text(json.dumps({"model": MODEL, "total": len(items), "found": len(titles),
                                  "minutes": round((time.time() - t0) / 60, 1), "usage": tot,
                                  "cost_usd_est": round(cost, 2),
                                  "missing": {u: {k: r.get(k) for k in ("note", "src")}
                                              for u, r in results.items() if not r.get("title")}},
                                 ensure_ascii=False, indent=1))
    print(f"찾음 {len(titles)}/{len(items)}, 추정 비용 ${cost:.2f}")


if __name__ == "__main__":
    main()
