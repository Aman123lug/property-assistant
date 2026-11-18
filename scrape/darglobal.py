"""Scrape darglobal.co.uk into data/raw/darglobal_pages.jsonl.

  python scrape/darglobal.py            # every page in the sitemap (~480), resumes if re-run
  python scrape/darglobal.py --limit 20 # a quick check

Source of URLs: https://darglobal.co.uk/sitemap.xml (robots.txt allows /, /projects/, /blog/, /press/).
Per page: the page's own data (__NEXT_DATA__) plus its visible article text.
Kept per page: title, description, text, every content image with its caption (the CMS alt text or the
nearest heading), and for project pages the structured facts (location, unit types, sizes, amenities,
distances, coordinates) and the unit inventory from /product-category/<slug> when units are listed.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request

from playwright.async_api import async_playwright

SITEMAP = "https://darglobal.co.uk/sitemap.xml"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "darglobal_pages.jsonl")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130 Safari/537.36"
IMG = re.compile(r"\.(jpe?g|png|webp)$", re.I)
SKIP_IMG = re.compile(r"logo|icon|favicon|placeholder", re.I)
CAPTION_KEYS = ("alt", "Title", "title", "Heading", "ProjectTitle", "heading")
# Sections that show OTHER projects or site chrome: their images must not be filed under this page.
NOT_THIS_PAGE = {"OtherProject", "otherProjectData", "globalData", "thankyouPageData", "thankyouData"}
TEXT_JS = """() => {
  const el = document.querySelector('article') || document.querySelector('main') || document.body;
  let t = el.innerText || '';
  for (const cut of ['REGISTER YOUR INTEREST', 'PLEASE PROVIDE YOUR DETAILS']) { const i = t.indexOf(cut); if (i > 200) t = t.slice(0, i); }
  return t.replace(/\\n{3,}/g, '\\n\\n').trim();
}"""


# Unit records also carry the developer's bank account details (IBAN, account number, SWIFT): keep only these.
UNIT_FIELDS = ("uniqueId", "unitCode", "unitName", "unitType", "unitView", "unitCategory", "floor", "furnished",
               "parkingSlot", "noOfBedrooms", "noOfBathrooms", "saleableAreasqm", "saleableAreasqft", "unitPrice",
               "projectCurrency", "projectName", "projectCode", "projectCity", "projectCountry", "projectLatitude",
               "projectLongitude", "propertyStatus", "inventoryStatus", "anticipatedCompletionDate", "usageType")


def keep_unit(u):
    a = u.get("attributes", u) if isinstance(u, dict) else {}
    return {"id": u.get("id") if isinstance(u, dict) else None, "attributes": {k: a.get(k) for k in UNIT_FIELDS if k in a}}


def s(v):
    return v.strip() if isinstance(v, str) and v.strip() and v.strip() not in ("None", "null") else None


def images(o, caption=None, out=None):
    """Every CMS image under o, captioned by its own alt text or the nearest heading above it."""
    out = [] if out is None else out
    if isinstance(o, dict):
        cap = next((s(o[k]) for k in CAPTION_KEYS if s(o.get(k))), caption)
        url = o.get("url")
        if isinstance(url, str) and IMG.search(url) and not SKIP_IMG.search(url):
            alt = s(o.get("alternativeText")) or s(o.get("caption"))
            out.append({"url": url, "caption": alt if alt and not IMG.search(alt) else cap})
        for k, v in o.items():
            if k not in NOT_THIS_PAGE:
                images(v, cap, out)
    elif isinstance(o, list):
        for v in o:
            images(v, caption, out)
    return out


def strip_html(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()


def project_facts(pp):
    d = ((pp.get("projectDetailsData") or {}).get("attributes") or {})
    pd = d.get("ProjectDetails") or {}
    if not pd:
        return None
    g = lambda *ks: _dig(pd, ks)  # noqa: E731
    loc = g("loaction", "locationDetailsWithImages") or {}
    return {
        "name": s(g("ProjectBanner", "Heading")) or s(d.get("title")),
        "slug": d.get("slug"),
        "location": s(g("ProjectBanner", "Location")),
        "area": s(g("ProjectBanner", "subheading")),
        "headline": s(g("AboutProject", "title")),
        "about": strip_html(g("AboutProject", "richDescription")),
        "details": {s(x.get("name")): s(x.get("value")) for x in g("AboutProject", "AboutDetailsType") or [] if s(x.get("name"))},
        "amenities": [s(x.get("name")) for x in g("Amenities", "amenitiesList") or [] if s(x.get("name"))],
        "nearby": [{"place": s(x.get("Title")), "time": s(x.get("Duration"))} for x in g("KeyFeatures", "keyfeaturelist") or []],
        "why_invest": [f"{s(x.get('name'))}: {s(x.get('value'))}" for x in g("whyInvest", "investlist") or [] if s(x.get("name"))],
        "location_text": strip_html(g("loaction", "description")),
        "lat": loc.get("lat"), "lon": loc.get("long"),
        "faq": [{"q": s(x.get("question") or x.get("Question")), "a": strip_html(x.get("answer") or x.get("Answer"))}
                for x in d.get("faqContent") or [] if isinstance(x, dict)],
        "book_unit": s(g("bookunit", "href")),
    }


def _dig(o, ks):
    for k in ks:
        o = o.get(k) if isinstance(o, dict) else None
    return o


async def read(page, url, retry=True):
    try:
        r = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:  # noqa: BLE001 -- network blip or sleep: one retry, then skip the page
        if not retry:
            print(f"  failed {url}: {type(e).__name__}", file=sys.stderr)
            return None, "error"
        await asyncio.sleep(30)
        return await read(page, url, retry=False)
    await page.wait_for_timeout(1500)
    raw = await page.evaluate("() => document.getElementById('__NEXT_DATA__')?.textContent || ''")
    if not raw:
        return None, r.status if r else None
    return json.loads(raw), r.status


async def main(args):
    req = urllib.request.Request(SITEMAP, headers={"User-Agent": UA})
    urls = re.findall(r"<loc>([^<]+)</loc>", urllib.request.urlopen(req, timeout=60).read().decode())
    urls = [u for u in urls if "/pay-online" not in u and "/landing-page" not in u]
    done, projects = set(), set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                done.add(r["url"])
                if r.get("section") == "project":
                    projects.add(r["url"])
    if args.refetch_projects:
        todo = sorted(projects)
    else:
        todo = [u for u in urls if u.rstrip("/") not in done][: args.limit or None]
    print(f"sitemap {len(urls)} pages, {len(done)} done, fetching {len(todo)}", file=sys.stderr)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(user_agent=UA)
        blocked = 0
        with open(OUT, "a", encoding="utf-8") as out:
            for n, url in enumerate(todo, 1):
                nd, status = await read(page, url)
                if status == "error":
                    continue
                if nd is None:  # the bot check: back off once, then give up for this run (re-run resumes)
                    blocked += 1
                    print(f"  blocked ({status}) {url}", file=sys.stderr)
                    if blocked >= 3:
                        print("Blocked 3 times: stopping. Wait a while and re-run to resume.", file=sys.stderr)
                        break
                    await asyncio.sleep(60)
                    continue
                blocked = 0
                pp = nd["props"]["pageProps"]
                row = {
                    "url": url.rstrip("/"),
                    "section": url.split("/")[3] if len(url.split("/")) > 4 else "page",
                    "title": await page.title(),
                    "description": await page.evaluate("() => document.querySelector('meta[name=description]')?.content || ''"),
                    "text": (await page.evaluate(TEXT_JS))[:20000],
                    "images": list({i["url"]: i for i in images(pp)}.values())[:60],
                    "project": project_facts(pp),
                }
                if row["project"] and row["project"]["slug"]:
                    row["section"] = "project"
                    inv, _ = await read(page, f"https://darglobal.co.uk/product-category/{row['project']['slug']}")
                    row["units"] = [keep_unit(u) for u in (inv or {}).get("props", {}).get("pageProps", {}).get("invData") or []]
                    await asyncio.sleep(args.delay)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                if n % 25 == 0:
                    print(f"{n}/{len(todo)}", file=sys.stderr)
                await asyncio.sleep(args.delay)
        await browser.close()
    print(f"done -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between pages")
    ap.add_argument("--refetch-projects", action="store_true", help="fetch project pages again (after a parser change)")
    asyncio.run(main(ap.parse_args()))
