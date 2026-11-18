"""Read side of the knowledge base: the tools the agent calls.

One SQLite file, built offline by app/build_kb.py:
- listings: one row per property, typed columns, so price/bedroom/area questions are SQL, not similarity;
- projects: DarGlobal projects with their structured fields;
- docs: FTS5 (BM25) over every page and listing description, for questions that are about text.
"""
import os
import re
import sqlite3

from app.text import norm

DB_PATH = os.environ.get("KB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "kb.sqlite"))

_conn = None


def conn():
    """One read-only connection. immutable=1: the file never changes while the app runs, so SQLite skips locking.
    Every caller is on the event loop thread (the tools are sync and quick), never on the thread pool."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def meta():
    return {r["key"]: r["value"] for r in conn().execute("SELECT key, value FROM meta")}


_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def number(v, name):
    """Model arguments arrive as 3, "3", "2,000,000" or "2M". Anything else is an error the model sees and fixes,
    never a silent text-vs-REAL comparison (which in SQLite matches every row)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    m = re.fullmatch(r"\s*([\d.,]+)\s*([a-zA-Z]*)\s*", str(v))
    if m and m.group(2).lower() in ("", *_SCALE):
        try:
            return float(m.group(1).replace(",", "")) * _SCALE.get(m.group(2).lower(), 1)
        except ValueError:
            pass
    raise ValueError(f"{name} must be a number, got {v!r}")


def _where(f):
    """Shared filter builder for listings. Every value is bound, never formatted into SQL."""
    sql, args = [], []
    for col in ("source", "property_for", "city", "district", "sub_type", "price_period", "project"):
        if f.get(col):
            sql.append(f"{col} LIKE ?")
            args.append(f"%{f[col]}%")
    for key, col, op in (("min_price", "price", ">="), ("max_price", "price", "<="),
                         ("min_bedrooms", "bedrooms", ">="), ("max_bedrooms", "bedrooms", "<="),
                         ("min_area_sqm", "area_sqm", ">="), ("max_area_sqm", "area_sqm", "<=")):
        if f.get(key) not in (None, ""):
            sql.append(f"{col} {op} ?")
            args.append(number(f[key], key))
    if f.get("text"):
        sql.append("(title LIKE ? OR description LIKE ?)")
        args += [f"%{f['text']}%"] * 2
    return (" WHERE " + " AND ".join(sql)) if sql else "", args


SORTS = {"price_asc": "price IS NULL, price ASC", "price_desc": "price IS NULL, price DESC",
         "area_desc": "area_sqm IS NULL, area_sqm DESC", "newest": "published_at DESC"}
COLS = ("id, source, url, title, project, property_for, sub_type, city, district, price, price_period, currency, "
        "status, bedrooms, bathrooms, area_sqm, cover")


def search_listings(limit=8, sort=None, **filters):
    where, args = _where(filters)
    order = f" ORDER BY {SORTS[sort]}" if sort in SORTS else ""
    limit = max(1, min(int(number(limit or 8, "limit")), 15))
    rows = conn().execute(f"SELECT {COLS} FROM listings{where}{order} LIMIT ?", args + [limit]).fetchall()
    total = conn().execute(f"SELECT COUNT(*) FROM listings{where}", args).fetchone()[0]
    return {"matching_in_sample": total, "results": [dict(r) for r in rows]}


def listing_stats(group_by="city", **filters):
    """Prices are only comparable within one purpose (sale/rent), one period and one currency, so those are
    always part of the grouping: an average never mixes a sale price with a yearly rent, or SAR with OMR."""
    if group_by not in ("city", "district", "sub_type", "project"):
        group_by = "city"
    filters.pop("limit", None)
    filters.pop("sort", None)
    where, args = _where(filters)
    where += (" AND " if where else " WHERE ") + "price > 0"
    rows = conn().execute(
        f"SELECT {group_by} AS grp, property_for, price_period, currency, COUNT(*) AS n, ROUND(AVG(price)) AS avg_price, "
        f"MIN(price) AS min_price, MAX(price) AS max_price, ROUND(AVG(price / NULLIF(area_sqm, 0))) AS avg_price_per_sqm "
        f"FROM listings{where} GROUP BY grp, property_for, price_period, currency ORDER BY n DESC LIMIT 20", args).fetchall()
    return {"note": "Computed over the scraped sample, not the whole market. avg_price_per_sqm uses listings with an area.",
            "groups": [dict(r) for r in rows]}


def _owner(o):
    o = str(o or "").strip()
    return f"wasalt-{o}" if o.isdigit() else o


def get_listing(id):
    id = _owner(id)
    row = conn().execute("SELECT * FROM listings WHERE id = ?", (id,)).fetchone()
    if not row:
        return {"error": "No listing with that id."}
    d = dict(row)
    d["photo_labels"] = [r[0] for r in conn().execute(
        "SELECT DISTINCT label FROM images WHERE owner = ? AND label IS NOT NULL", (id,))]
    return d


def get_project(name):
    rows = conn().execute("SELECT * FROM projects WHERE name LIKE ? OR slug LIKE ? LIMIT 3",
                          (f"%{name}%", f"%{name}%")).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["owner"] = f"project-{d['slug']}"
        d["photo_captions"] = [x[0] for x in conn().execute(
            "SELECT DISTINCT label FROM images WHERE owner = ? AND label IS NOT NULL LIMIT 30", (d["owner"],))]
        d["units_for_sale"] = [dict(x) for x in conn().execute(
            "SELECT id, title, sub_type, price, currency, bedrooms, area_sqm, status, description FROM listings "
            "WHERE source = 'darglobal' AND url = ? ORDER BY price", (d["url"],))]
        out.append(d)
    return {"results": out} if out else {"results": [], "hint": "Try list_projects or search_docs."}


def photos(owner, label=None, limit=8):
    """Photos of one listing (owner = listing id) or project (owner = 'project-<slug>'), optionally by label."""
    owner = _owner(owner)
    sql, args = "SELECT url, thumb, label FROM images WHERE owner = ?", [owner]
    if label:
        sql += " AND label LIKE ?"
        args.append(f"%{label}%")
    rows = conn().execute(sql + " LIMIT ?", args + [max(1, min(int(number(limit or 8, "limit")), 12))]).fetchall()
    if not rows and not owner.startswith("wasalt-"):  # a project given by name or a guessed slug
        name = owner.removeprefix("project-").replace("-", " ")
        p = conn().execute("SELECT slug FROM projects WHERE slug = ? OR name LIKE ? OR replace(slug, '-', ' ') LIKE ? "
                           "LIMIT 1", (owner.removeprefix("project-"), f"%{name}%", f"%{name}%")).fetchone()
        if p and f"project-{p[0]}" != owner:
            return photos(f"project-{p[0]}", label, limit)
    title = None
    if owner.startswith("wasalt-"):
        t = conn().execute("SELECT title, price, currency, price_period FROM listings WHERE id = ?", (owner,)).fetchone()
        if t:
            per = {"yearly": " / year", "monthly": " / month", "daily": " / day"}.get(t["price_period"], "")
            title = f"{t['title']} - {t['currency']} {t['price']:,.0f}{per}" if t["price"] else t["title"]
    else:
        t = conn().execute("SELECT name FROM projects WHERE slug = ?", (owner.removeprefix("project-"),)).fetchone()
        title = t["name"] if t else None
    return {"owner": owner, "title": title, "photos": [dict(r) for r in rows]}


def list_projects(country=None, city=None):
    sql, args = "SELECT slug, name, city, country, area, units_listed, url, cover FROM projects", []
    cond = []
    for col, val in (("country", country), ("city", city)):
        if val:
            cond.append(f"{col} LIKE ?")
            args.append(f"%{val}%")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    return {"results": [dict(r) for r in conn().execute(sql + " ORDER BY country, name", args)]}


_WORD = re.compile(r"\w+", re.UNICODE)


def search_docs(query, source=None, kind=None, limit=6):
    """BM25 over page text. Each word is quoted, so user text can never be FTS5 syntax."""
    words = [w for w in _WORD.findall(norm(query)) if len(w) > 1][:12]
    words += [w[2:] for w in words if w.startswith("ال") and len(w) > 4]  # Arabic: match with and without "al-"
    if not words:
        return {"results": []}
    match = " OR ".join(f'"{w}"' for w in words)
    sql, args = ("SELECT url, title, source, kind, snippet(docs, 5, '', '', ' ... ', 60) AS excerpt "
                 "FROM docs WHERE docs MATCH ?"), [match]
    for col, val in (("source", source), ("kind", kind)):
        if val:
            sql += f" AND {col} = ?"
            args.append(val)
    sql += " ORDER BY bm25(docs, 0, 4.0, 0, 0, 1.0, 1.0) LIMIT ?"
    args.append(max(1, min(int(limit or 6), 10)))
    return {"results": [dict(r) for r in conn().execute(sql, args)]}


def get_page(url):
    row = conn().execute("SELECT url, title, source, kind, body FROM docs WHERE url = ? LIMIT 1", (url,)).fetchone()
    if not row:
        return {"error": "No page with that URL in the knowledge base."}
    d = dict(row)
    d["body"] = d["body"][:6000]
    return d
