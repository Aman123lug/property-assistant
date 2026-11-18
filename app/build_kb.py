"""Build data/kb.sqlite from the scraped files. Run after the scrapers: python -m app.build_kb

Tables:
  listings  one row per property with typed columns (price, bedrooms, area...), so filters are exact SQL;
            Wasalt listings plus DarGlobal units when a project lists them
  projects  DarGlobal projects: location, unit types, sizes, amenities, nearby places, coordinates
  images    every photo, mapped to its listing or project, with its label (Wasalt's room classification)
            or caption (DarGlobal's CMS alt text): the chatbot shows "the pool" by caption, not by guess
  docs      FTS5 (BM25) over page text in ~1500-char chunks; each chunk starts with its page title and section,
            so a chunk is findable by what page it belongs to (a cheap form of contextual chunk headers)
  meta      build date and counts
"""
import json
import os
import re
import sqlite3
import time

from app.text import norm, redact

ROOT = os.path.join(os.path.dirname(__file__), "..")
RAW = os.path.join(ROOT, "data", "raw")
DB = os.path.join(ROOT, "data", "kb.sqlite")
MIN_PAGE_CHARS = 400  # shorter pages are error stubs ("Application error") or empty shells

SCHEMA = """
CREATE TABLE listings (
  id TEXT PRIMARY KEY, source TEXT, url TEXT, title TEXT, project TEXT, property_for TEXT, sub_type TEXT,
  city TEXT, district TEXT, address TEXT, price REAL, price_period TEXT, currency TEXT, status TEXT,
  bedrooms INTEGER, bathrooms INTEGER, area_sqm REAL, age TEXT, facade TEXT, street_width TEXT,
  furnishing TEXT, description TEXT, lat REAL, lon REAL, published_at TEXT, cover TEXT
);
CREATE TABLE projects (
  slug TEXT PRIMARY KEY, url TEXT, name TEXT, city TEXT, country TEXT, area TEXT, headline TEXT,
  about TEXT, details TEXT, amenities TEXT, nearby TEXT, why_invest TEXT, lat REAL, lon REAL,
  units_available INTEGER DEFAULT 0, cover TEXT
);
CREATE TABLE images (owner TEXT, url TEXT, thumb TEXT, label TEXT);
CREATE INDEX images_owner ON images(owner);
CREATE INDEX listings_url ON listings(url);
CREATE VIRTUAL TABLE docs USING fts5(url UNINDEXED, title, source UNINDEXED, kind UNINDEXED, city, body,
                                     tokenize = 'porter unicode61 remove_diacritics 2');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def num(v):
    try:
        return float(str(v).replace(",", "").split()[0])
    except (ValueError, IndexError, TypeError):
        return None


def integer(v):
    """'3', 3.0, '7+' -> 3, 3, 7. Anything else -> None, never text in an INTEGER column."""
    m = re.match(r"\s*(\d+)", str(v)) if v is not None else None
    return int(m.group(1)) if m else None


def jsonl(name):
    path = os.path.join(RAW, name)
    if not os.path.exists(path):
        print(f"missing {path}, skipped")
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def chunks(text, size=1500):
    paras, out, cur = re.split(r"\n{2,}", text or ""), [], ""
    for p in paras:
        if len(cur) + len(p) > size and cur:
            out.append(cur)
            cur = ""
        cur += p + "\n\n"
    if cur.strip():
        out.append(cur)
    return out


def add_doc(db, url, title, source, kind, city, body):
    db.execute("INSERT INTO docs VALUES (?,?,?,?,?,?)", (url, norm(title), source, kind, norm(city), norm(body)))


def wasalt(db):
    seen = set()
    for r in jsonl("wasalt_listings.jsonl"):
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        attrs = {a["key"]: a.get("value") for a in r.get("attributes") or []}
        det = {a["key"]: a.get("value") for a in r.get("details") or []}
        if r["property_for"] == "rent":
            rents = {x["freq"]: num(x["amount"]) for x in r.get("rent") or [] if num(x.get("amount"))}
            period = next((f for f in ("yearly", "monthly", "daily") if f in rents), None)
            price = rents.get(period)
        else:
            period, price = "total", num(r.get("sale_price"))
        photos = r.get("photos") or []
        lid = f"wasalt-{r['id']}"
        title, desc = redact(r.get("title")), redact(r.get("description"))
        db.execute("INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            lid, "wasalt", r["url"], title, None, r.get("property_for"), r.get("sub_type"),
            r.get("city"), r.get("district"), r.get("address"), price or None, period, r.get("currency") or "SAR", None,
            integer(attrs.get("noOfBedrooms")), integer(attrs.get("noOfBathrooms")),
            num(attrs.get("builtUpArea")) or num(attrs.get("carpetArea")),
            det.get("completionYear"), det.get("propertyFacade"), det.get("propertyStreetWidth"), det.get("furnishingType"),
            desc, num(r.get("lat")), num(r.get("lon")), r.get("published_at"), photos[0]["thumb"] if photos else None))
        db.executemany("INSERT INTO images VALUES (?,?,?,?)",
                       [(lid, p["url"], p["thumb"], p.get("label")) for p in photos])
        add_doc(db, r["url"], title, "wasalt", "listing", r.get("city"),
                f"{title}. {r.get('sub_type')} for {r.get('property_for')} in {r.get('address')}. {desc}")
    return len(seen)


def darglobal(db):
    pages = list({p["url"]: p for p in jsonl("darglobal_pages.jsonl")}.values())  # last row per URL wins
    pages.sort(key=lambda p: not p.get("project"))  # project pages first, so they win text de-duplication
    n_pages, n_projects, seen_text, all_units = 0, 0, set(), {}
    for pg in pages:
        url, pj = pg["url"], pg.get("project")
        if pj and (pj.get("location") or "worldwide").strip().lower() == "worldwide":
            pj = None  # landing pages built on the project template ("Worldwide"...) are not projects
        for u in pg.get("units") or []:  # the inventory page shows a default unit for projects without stock,
            a = u.get("attributes", u) if isinstance(u, dict) else {}   # so units are keyed by their own id and
            uid = a.get("uniqueId") or a.get("unitCode") or u.get("id")  # project, never by the page they were on
            if uid:
                all_units.setdefault(str(uid), {**a, "_uid": str(uid)})
        text = redact(pg.get("text"))
        key = hash(text.strip()[:3000])
        if not pj and (len(text) < MIN_PAGE_CHARS or key in seen_text):
            continue  # error stubs and duplicate copies of the home page
        seen_text.add(key)
        n_pages += 1
        owner = url
        if pj and pj.get("slug"):
            n_projects += 1
            owner = f"project-{pj['slug']}"
            loc = [x.strip() for x in pj["location"].split(",")]
            loc = loc if len(loc) > 1 else [None, loc[0]]  # "Saudi Arabia" alone is a country
            imgs = pg.get("images") or []
            db.execute("INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)", (
                pj["slug"], url, pj.get("name"), loc[0], country(loc[-1]), pj.get("area"), pj.get("headline"), pj.get("about"),
                json.dumps(pj.get("details"), ensure_ascii=False), ", ".join(pj.get("amenities") or []),
                "; ".join(f"{x['place']} {x['time']}" for x in pj.get("nearby") or [] if x.get("place")),
                "; ".join(pj.get("why_invest") or []), num(pj.get("lat")), num(pj.get("lon")),
                imgs[0]["url"] if imgs else None))
        db.executemany("INSERT INTO images VALUES (?,?,?,?)",
                       [(owner, i["url"], i["url"], i.get("caption")) for i in pg.get("images") or []])
        facts = ""
        if pj:
            facts = "\n".join(filter(None, [
                f"Project: {pj.get('name')} in {pj.get('area') or ''} {pj.get('location') or ''}",
                "; ".join(f"{k}: {v}" for k, v in (pj.get("details") or {}).items()),
                pj.get("about"), "Amenities: " + ", ".join(pj.get("amenities") or []),
                "Nearby: " + "; ".join(f"{x['place']} {x['time']}" for x in pj.get("nearby") or [] if x.get("place")),
                "Why invest: " + "; ".join(pj.get("why_invest") or []), pj.get("location_text"),
                "\n".join(f"Q: {f['q']} A: {f['a']}" for f in pj.get("faq") or [] if f.get("q")),
            ])) + "\n\n"
        title = (pg.get("title") or "").replace(" | DarGlobal", "")
        header = f"[DarGlobal {pg.get('section')}: {title}]\n"
        for c in chunks(facts + text):
            add_doc(db, url, title, "darglobal", "project" if pj else pg.get("section"), None, header + c)
    n_units = units(db, all_units.values())
    return n_pages, n_projects, n_units


COUNTRY = {"uae": "United Arab Emirates", "rak": "United Arab Emirates", "ksa": "Saudi Arabia",
           "oman - aida": "Oman", "uk": "United Kingdom"}


def country(c):
    return COUNTRY.get((c or "").strip().lower(), c)


def squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def units(db, rows):
    """DarGlobal units for sale -> listings. Only named fields are read: the source records also carry the
    developer's bank account details, which are never stored."""
    n = 0
    projects = db.execute("SELECT slug, url, name FROM projects").fetchall()
    for a in rows:
        if (a.get("inventoryStatus") or "").lower() != "available":
            continue
        name = a.get("projectName") or ""
        key = squash(name)
        match = next((p for p in projects if key and squash(p[2]).startswith(key)), None) or \
            next((p for p in projects if key and (key in squash(p[2]) or squash(p[2]) in key)), None)
        url = match[1] if match else "https://darglobal.co.uk/bookYourUnit"
        facts = "; ".join(f"{k}: {a[f]}" for k, f in (
            ("Unit", "unitName"), ("Type", "unitType"), ("View", "unitView"), ("Floor", "floor"),
            ("Furnished", "furnished"), ("Parking", "parkingSlot"), ("Completion", "anticipatedCompletionDate"),
            ("Status", "propertyStatus"), ("Area sqft", "saleableAreasqft")) if a.get(f) not in (None, "", "None"))
        db.execute("INSERT OR IGNORE INTO listings (id, source, url, title, project, property_for, sub_type, city, price, "
                   "price_period, currency, status, bedrooms, bathrooms, area_sqm, description, lat, lon) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                       f"darglobal-{a['_uid']}", "darglobal", url, f"{name} {a.get('unitType') or 'unit'} {a.get('unitName') or ''}".strip(),
                       name, "sale", a.get("unitType"), a.get("projectCity"), num(a.get("unitPrice")), "total",
                       a.get("projectCurrency"), a.get("propertyStatus"), integer(a.get("noOfBedrooms")),
                       integer(a.get("noOfBathrooms")), num(a.get("saleableAreasqm")), facts,
                       num(a.get("projectLatitude")), num(a.get("projectLongitude"))))
        if match:
            db.execute("UPDATE projects SET units_available = units_available + 1 WHERE slug = ?", (match[0],))
        n += 1
    db.execute("UPDATE listings SET cover = (SELECT cover FROM projects WHERE projects.url = listings.url) "
               "WHERE source = 'darglobal'")
    return n


def main():
    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    n_pages, n_projects, n_units = darglobal(db)
    n_listings = wasalt(db)
    counts = {"wasalt_listings": n_listings, "darglobal_pages": n_pages, "darglobal_projects": n_projects,
              "darglobal_units": n_units, "images": db.execute("SELECT COUNT(*) FROM images").fetchone()[0],
              "doc_chunks": db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]}
    meta = {"built_at": time.strftime("%Y-%m-%d"), **{k: str(v) for k, v in counts.items()}}
    db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, DB)
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
