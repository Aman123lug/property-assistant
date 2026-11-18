"""The chat agent: one tool-calling loop over the knowledge base.

Retrieval is chosen by the model per question, not fixed:
- requirement and price questions ("3-bed villa in Riyadh under 2M") -> search_listings / listing_stats (exact SQL);
- DarGlobal projects -> list_projects / get_project (structured facts);
- anything written in prose (blog, press, project descriptions) -> search_docs (BM25);
- "show me the kitchen / the pool" -> photos (images mapped to their listing or project by label/caption).
The system prompt asks the model to check that every part of the question is covered and search again if
not (the "sufficient context" step of Google's agentic RAG, done inside the same loop to save free-tier calls).
"""
import json
import os
import re
import time
from datetime import datetime, timezone

from openai import AsyncOpenAI

from app import kb

MODELS = [m.strip() for m in os.environ.get(
    "OPENROUTER_MODELS",
    "nvidia/nemotron-3-ultra-550b-a55b:free,nvidia/nemotron-3.5-super-vl:free,google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free").split(",") if m.strip()]
MAX_TOOL_ROUNDS = 3
DAILY_MODEL_CALLS = int(os.environ.get("DAILY_MODEL_CALLS", "45"))  # free tier: ~50 requests/day per account

_client = None
_budget = {"day": None, "used": 0}
_cache = {}


def client():
    global _client
    if _client is None:
        _client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ.get("OPENROUTER_API_KEY", ""),
                              timeout=60, max_retries=0)
    return _client


def _prop(t, d):
    return {"type": t, "description": d}


LISTING_FILTERS = {
    "property_for": {"type": "string", "enum": ["sale", "rent"]},
    "city": _prop("string", "City as stored, e.g. Riyadh, Jeddah, Aldammam, Khobar, Dubai."),
    "district": _prop("string", "District / neighbourhood, partial match."),
    "sub_type": _prop("string", "Apartment, Villa, Floor, Land, Building, Rest House, Office, Shop..."),
    "min_price": _prop("number", "In the listing currency (SAR for Wasalt). Rent prices are per price_period."),
    "max_price": _prop("number", "See min_price."),
    "price_period": {"type": "string", "enum": ["total", "yearly", "monthly", "daily"]},
    "min_bedrooms": _prop("integer", ""), "max_bedrooms": _prop("integer", ""),
    "min_area_sqm": _prop("number", ""), "max_area_sqm": _prop("number", ""),
    "text": _prop("string", "One word or short phrase that must appear in title/description, e.g. 'pool', 'furnished'."),
    "source": {"type": "string", "enum": ["wasalt", "darglobal"]},
    "project": _prop("string", "DarGlobal project name, for its units for sale."),
}


def _tool(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}}


TOOLS = [
    _tool("search_listings", "Find properties matching requirements. Returns rows with price, bedrooms, area, url, id.",
          {**LISTING_FILTERS, "sort": {"type": "string", "enum": ["price_asc", "price_desc", "area_desc", "newest"]},
           "limit": _prop("integer", "Max rows, default 8.")}),
    _tool("listing_stats", "Count / average / min / max price (and price per sqm) of listings, grouped. "
          "Use for 'how much does X cost on average', 'which district is cheapest'.",
          {**LISTING_FILTERS, "group_by": {"type": "string", "enum": ["city", "district", "sub_type", "project"]}}),
    _tool("get_listing", "Full details of one listing by its id (from search_listings).", {"id": _prop("string", "")}, ["id"]),
    _tool("list_projects", "List DarGlobal projects, optionally by country or city.",
          {"country": _prop("string", ""), "city": _prop("string", "")}),
    _tool("get_project", "Facts about one DarGlobal project: location, unit types, sizes, amenities, nearby places, "
          "why invest, photo captions.", {"name": _prop("string", "Project name or part of it.")}, ["name"]),
    _tool("search_docs", "Keyword (BM25) search over DarGlobal pages (projects, blog, press, insights) and listing "
          "descriptions. Use several specific keywords, including synonyms.",
          {"query": _prop("string", ""), "source": {"type": "string", "enum": ["darglobal", "wasalt"]},
           "kind": _prop("string", "project, blog, press, insights, listing...")}, ["query"]),
    _tool("get_page", "Full text of one page by URL (from search_docs).", {"url": _prop("string", "")}, ["url"]),
    _tool("photos", "Photos to show the user. owner = a listing id (e.g. 'wasalt-5841671') or 'project-<slug>'. "
          "label narrows by room/caption, e.g. Kitchen, Facade, Bathroom, Pool, Living.",
          {"owner": _prop("string", ""), "label": _prop("string", ""), "limit": _prop("integer", "")}, ["owner"]),
]
IMPL = {"search_listings": kb.search_listings, "listing_stats": kb.listing_stats, "get_listing": kb.get_listing,
        "list_projects": kb.list_projects, "get_project": kb.get_project, "search_docs": kb.search_docs,
        "get_page": kb.get_page, "photos": kb.photos}


def system_prompt():
    m = kb.meta()
    cities = [r[0] for r in kb.conn().execute(
        "SELECT city FROM listings WHERE city IS NOT NULL GROUP BY city ORDER BY COUNT(*) DESC LIMIT 25")]
    types = [r[0] for r in kb.conn().execute(
        "SELECT sub_type FROM listings WHERE sub_type IS NOT NULL GROUP BY sub_type ORDER BY COUNT(*) DESC LIMIT 15")]
    return f"""You are a property advisor for DarGlobal (an international luxury developer: projects in the UAE,
Saudi Arabia, Oman, Qatar, Spain, the UK and the Maldives) and Wasalt (a Saudi property portal with listings for
sale and rent). Help the user find a property that fits their requirements and understand prices.

Knowledge base snapshot from {m.get('built_at')}: {m.get('wasalt_listings')} Wasalt listings (a sample of the
portal, not all of it), {m.get('darglobal_projects')} DarGlobal projects, {m.get('darglobal_pages')} DarGlobal pages.
Cities in the listings (use these spellings): {', '.join(cities)}.
Property types: {', '.join(types)}.

How to work:
1. Use the tools for every fact. Never invent a price, size, location or listing.
2. Requirements -> search_listings with filters. Wasalt prices are in SAR; DarGlobal units are priced in their
   project's currency (AED, SAR, OMR, EUR...): never compare or average across currencies. If nothing matches, relax one filter at a time and say which.
   If key requirements are missing (sale or rent, city, budget), search with what you have, then ask one short
   follow-up question.
   DarGlobal project questions -> get_project first: it already includes the units for sale and photo captions.
   Call independent tools together in the same turn (e.g. get_project and photos, or two searches), you have
   only {MAX_TOOL_ROUNDS} rounds.
3. Before answering, check every part of the question is covered by tool results. If a part is missing, search
   again with different keywords or filters (up to {MAX_TOOL_ROUNDS} rounds). If it is still missing, say so.
4. When the user wants to see a property, a room or a project, call photos. The photos are shown under your answer.
5. Answer in the user's language (Arabic or English). Be concise: a short summary, then the best 3-5 options as a
   list with price (currency, and per year/month for rent), bedrooms, size in sqm and district.
6. Link every property or project you mention as a markdown link to its url, e.g. [Villa in Al-Mahdiyah](url).
   Cards with photos are added automatically for the links you use.
7. Counts and averages come from a sample: say "in our sample".
8. You give information, not financial or legal advice."""


def _card(url):
    """A project URL is a project card (its units share that URL); a listing URL is a listing card."""
    r = kb.conn().execute("SELECT slug, name AS title, url, city, country, area, details, cover, units_available "
                          "FROM projects WHERE url = ?", (url.rstrip("/"),)).fetchone()
    if r:
        return {"kind": "project", **dict(r)}
    r = kb.conn().execute("SELECT id, source, title, url, price, price_period, currency, bedrooms, area_sqm, city, "
                          "district, sub_type, property_for, cover FROM listings WHERE url = ? AND source = 'wasalt' "
                          "LIMIT 1", (url,)).fetchone()
    return {"kind": "listing", **dict(r)} if r else None


def _summary(name, args):
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, ""))
    return f"{name}({shown})"


def _spend():
    today = datetime.now(timezone.utc).date()
    if _budget["day"] != today:
        _budget.update(day=today, used=0)
    if _budget["used"] >= DAILY_MODEL_CALLS:
        return False
    _budget["used"] += 1
    return True


async def _complete(messages, tools, state):
    """One model call, falling back across free models on rate limits and errors."""
    last = None
    for model in ([state["model"]] if state.get("model") else []) + [m for m in MODELS if m != state.get("model")]:
        if not _spend():
            raise RuntimeError("budget")
        try:
            kw = {"tools": tools, "tool_choice": "auto"} if tools else {}
            r = await client().chat.completions.create(
                model=model, messages=messages, temperature=0.2, max_tokens=1500,
                extra_headers={"X-Title": "DarGlobal & Wasalt property assistant"}, **kw)
            msg = r.choices[0].message if r.choices else None
            if msg is None or (not msg.content and not msg.tool_calls):
                raise ValueError("empty reply")
            state["model"] = model
            return msg
        except Exception as e:  # noqa: BLE001 -- any provider failure: try the next free model
            last = e
            state.pop("model", None)
    print(f"all models failed, last error: {type(last).__name__}: {str(last)[:300]}")
    raise RuntimeError("unavailable")


async def run(message, history=()):
    """Yields events: status, gallery, answer, cards, done, error."""
    key = message.strip().lower()
    if not history and key in _cache:
        for ev in _cache[key]:
            yield ev
        return
    if not os.environ.get("OPENROUTER_API_KEY"):
        yield {"type": "error", "text": "The server has no model key configured (OPENROUTER_API_KEY)."}
        return
    t0, out, state, galleries = time.time(), [], {}, []
    messages = [{"role": "system", "content": system_prompt()}]
    messages += [{"role": h["role"], "content": str(h["content"])[:4000]} for h in list(history)[-6:]
                 if h.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": message[:2000]})

    def emit(ev):
        out.append(ev)
        return ev

    try:
        for rnd in range(MAX_TOOL_ROUNDS + 1):
            if rnd == MAX_TOOL_ROUNDS:  # out of rounds: answer from what was found
                messages.append({"role": "user", "content": "Write the final answer now from the tool results above. "
                                 "No more tool calls. Say plainly if something could not be found."})
            msg = await _complete(messages, TOOLS if rnd < MAX_TOOL_ROUNDS else None, state)
            if not msg.tool_calls:
                break
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    result = IMPL[name](**args)
                except Exception as e:  # noqa: BLE001 -- bad tool arguments go back to the model to fix
                    args, result = {}, {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                yield emit({"type": "status", "text": _summary(name, args)})
                if name == "photos" and result.get("photos"):
                    galleries.append(result)
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": name,
                                 "content": json.dumps(result, ensure_ascii=False, default=str)[:12000]})
        # Some models write a tool call as text when tools are withdrawn: never show that to the user.
        answer = re.sub(r"<tool_call>.*?(</tool_call>|$)", "", msg.content or "", flags=re.S).strip()
        if not answer:
            answer = "I could not finish that search. Please ask again, a little more specifically."
        yield emit({"type": "answer", "text": answer})
        for g in galleries:
            yield emit({"type": "gallery", **g})
        urls = list(dict.fromkeys(re.findall(r"\]\((https?://[^)\s]+)\)", answer)))
        cards = [c for c in (_card(u) for u in urls[:8]) if c]
        if cards:
            yield emit({"type": "cards", "items": cards[:6]})
        yield emit({"type": "done", "model": state.get("model"), "seconds": round(time.time() - t0, 1)})
        if not history and len(_cache) < 300:
            _cache[key] = out
    except RuntimeError as e:
        text = ("Today's free model quota is used up. Please try again tomorrow." if str(e) == "budget"
                else "The free models are busy right now. Please try again in a minute.")
        yield {"type": "error", "text": text}
    except Exception as e:  # noqa: BLE001 -- the UI must always get an end to the turn
        print(f"turn failed: {type(e).__name__}: {e}")
        yield {"type": "error", "text": "Something went wrong on our side. Please try again."}
