"""Golden-set evaluation, checked against SQL ground truth (no LLM judge, so no extra model calls).

  python -m eval.run                 # all questions, resumes: answered questions are not asked again
  python -m eval.run --only dg-w-burj
  python -m eval.run --report        # rewrite eval/results.md from eval/results.jsonl

Checks per question (only those the question defines):
  links_exist   every linked URL is in the knowledge base (no invented links)        -- always
  valid         every linked Wasalt/DarGlobal listing satisfies the question's filters (valid_sql)
  links_in      every linked URL is in links_sql (e.g. only Dubai projects)
  min_links / max_links
  numbers       each number from numbers_sql appears in the reply within 3%
  expect_any    the reply mentions at least one of these phrases
  gallery       photos were shown
  language      the reply is in the question's language
"""
import argparse
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import agent, kb  # noqa: E402

HERE = os.path.dirname(__file__)
ENV = os.path.join(HERE, "..", ".env")
if os.path.exists(ENV):  # local runs: the key lives in .env (never committed)
    for line in open(ENV, encoding="utf-8"):
        k, _, v = line.strip().partition("=")
        if k and not k.startswith("#") and v:
            os.environ.setdefault(k, v)
QUESTIONS, RESULTS, REPORT = (os.path.join(HERE, f) for f in ("questions.jsonl", "results.jsonl", "results.md"))


def urls_of(sql):
    return {r[0].rstrip("/") for r in kb.conn().execute(sql)}


def numbers_in(text):
    vals = []
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|million|mn|مليون)?", text.replace("٬", ",").lower()):
        v = float(m.group(1).replace(",", ""))
        vals.append(v * {"k": 1e3, "m": 1e6, "million": 1e6, "mn": 1e6, "مليون": 1e6}.get(m.group(2) or "", 1))
    return vals


def grade(q, events):
    answer = next((e["text"] for e in events if e["type"] == "answer"), "")
    error = next((e["text"] for e in events if e["type"] == "error"), None)
    links = {u.rstrip("/") for u in re.findall(r"\]\((https?://[^)\s]+)\)", answer)}
    known = urls_of("SELECT url FROM listings UNION SELECT url FROM projects UNION SELECT url FROM docs")
    checks = {"answered": bool(answer) and not error, "links_exist": links <= known}
    listing_links = links & urls_of("SELECT url FROM listings")
    if "valid_sql" in q:
        checks["valid"] = listing_links <= urls_of(q["valid_sql"])
    if "links_sql" in q:
        checks["links_in"] = links <= urls_of(q["links_sql"])
    if "min_links" in q:
        checks["min_links"] = len(links) >= q["min_links"]
    if "max_links" in q:
        checks["max_links"] = len(links) <= q["max_links"]
    if "numbers_sql" in q:
        want = [r[0] for r in kb.conn().execute(q["numbers_sql"]) if r[0]]
        got = numbers_in(answer)
        checks["numbers"] = bool(want) and all(any(abs(g - w) <= 0.03 * w for g in got) for w in want)
    if "expect_any" in q:
        checks["expect_any"] = any(p.lower() in answer.lower() for p in q["expect_any"])
    if q.get("gallery"):
        checks["gallery"] = any(e["type"] == "gallery" and e.get("photos") for e in events)
    arabic = len(re.findall(r"[؀-ۿ]", answer)) / max(1, len(re.findall(r"\w", answer)))
    checks["language"] = (arabic > 0.3) if q["lang"] == "ar" else (arabic < 0.3)
    done = next((e for e in events if e["type"] == "done"), {})
    return {"id": q["id"], "q": q["q"], "pass": all(checks.values()), "checks": checks, "links": sorted(links),
            "model": done.get("model"), "seconds": done.get("seconds"), "error": error, "answer": answer[:1500],
            "tools": [e["text"] for e in events if e["type"] == "status"]}


def report():
    rows = [json.loads(line) for line in open(RESULTS, encoding="utf-8")] if os.path.exists(RESULTS) else []
    rows = list({r["id"]: r for r in rows}.values())
    ok = sum(r["pass"] for r in rows)
    lines = [f"# Evaluation results\n\n{ok}/{len(rows)} questions pass every check.\n",
             "| id | pass | failed checks | tool calls | model | s |", "|---|---|---|---|---|---|"]
    for r in rows:
        failed = ", ".join(k for k, v in r["checks"].items() if not v) or "-"
        lines.append(f"| {r['id']} | {'yes' if r['pass'] else 'no'} | {failed} | {len(r['tools'])} | {r['model']} | {r['seconds']} |")
    open(REPORT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))


async def main(args):
    qs = [json.loads(line) for line in open(QUESTIONS, encoding="utf-8") if line.strip()]
    done = set()
    if os.path.exists(RESULTS):
        done = {json.loads(line)["id"] for line in open(RESULTS, encoding="utf-8") if json.loads(line).get("error") is None}
    for q in qs:
        if (args.only and q["id"] != args.only) or (not args.only and q["id"] in done):
            continue
        agent._cache.clear()
        events = [ev async for ev in agent.run(q["q"])]
        r = grade(q, events)
        print(f"{r['id']}: {'PASS' if r['pass'] else 'FAIL'} {r['checks']}")
        with open(RESULTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if r["error"] and "quota" in r["error"]:
            print("Daily free quota used up: re-run tomorrow to continue (answered questions are skipped).")
            break
    report()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    report() if a.report else asyncio.run(main(a))
