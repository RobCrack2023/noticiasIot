import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
import feedparser
import httpx
from deep_translator import GoogleTranslator
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("techpulse")

_translate_cache: dict[str, str] = {}
_executor = ThreadPoolExecutor(max_workers=4)

PLACEHOLDERS = {
    "tech":     "/static/img/placeholder-tech.svg",
    "iot":      "/static/img/placeholder-iot.svg",
    "robotics": "/static/img/placeholder-robotics.svg",
    "latam":    "/static/img/placeholder-latam.svg",
}
REFRESH_INTERVAL = 300  # seconds


async def _prefetch_all():
    """Warm up the RSS cache for all categories."""
    log.info("Prefetching all feeds…")
    await get_all_news(category="all")
    log.info("Feed cache warm.")


async def _background_refresh():
    """Daemon: initial warm-up then refresh every REFRESH_INTERVAL seconds."""
    await _prefetch_all()
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        try:
            await _prefetch_all()
        except Exception as e:
            log.warning("Background refresh error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_background_refresh())
    log.info("Background refresh daemon started.")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.info("Background refresh daemon stopped.")


app = FastAPI(title="TechPulse - Noticias IoT & Robótica", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

RSS_SOURCES = {
    "all": [
        # ── Internacional (inglés) ──────────────────────────────
        {"name": "Hackaday",          "url": "https://hackaday.com/blog/feed/",                        "category": "iot",      "lang": "en"},
        {"name": "IEEE Spectrum",     "url": "https://spectrum.ieee.org/feeds/feed.rss",                "category": "robotics", "lang": "en"},
        {"name": "TechCrunch",        "url": "https://techcrunch.com/feed/",                            "category": "tech",     "lang": "en"},
        {"name": "Wired",             "url": "https://www.wired.com/feed/rss",                          "category": "tech",     "lang": "en"},
        {"name": "Ars Technica",      "url": "https://feeds.arstechnica.com/arstechnica/index",         "category": "tech",     "lang": "en"},
        {"name": "The Verge",         "url": "https://www.theverge.com/rss/index.xml",                  "category": "tech",     "lang": "en"},
        {"name": "MIT Tech Review",   "url": "https://www.technologyreview.com/stories.rss",            "category": "tech",     "lang": "en"},
        {"name": "RoboHub",           "url": "https://robohub.org/feed/",                               "category": "robotics", "lang": "en"},
        {"name": "Electronics Weekly","url": "https://www.electronicsweekly.com/feed/",                  "category": "iot",      "lang": "en"},
        {"name": "Tom's Hardware",    "url": "https://www.tomshardware.com/feeds/all",                  "category": "tech",     "lang": "en"},
        # ── Latinoamérica (español) ────────────────────────────
        {"name": "Xataka México",     "url": "https://feeds.weblogssl.com/xatakamx",                   "category": "latam",    "lang": "es"},
        {"name": "Fayerwayer",        "url": "https://www.fayerwayer.com/feed/",                        "category": "latam",    "lang": "es"},
        {"name": "Enter.co",          "url": "https://www.enter.co/feed/",                              "category": "latam",    "lang": "es"},
        {"name": "Hipertextual",      "url": "https://hipertextual.com/feed",                           "category": "latam",    "lang": "es"},
        {"name": "IoT LATAM",         "url": "https://iotlatam.com/feed/",                              "category": "latam",    "lang": "es"},
        {"name": "Digital Trends ES", "url": "https://es.digitaltrends.com/feed/",                      "category": "latam",    "lang": "es"},
        {"name": "Infobae Tecno",     "url": "https://www.infobae.com/feeds/rss/tecnologia/",           "category": "latam",    "lang": "es"},
        {"name": "La Nación Tech",    "url": "https://www.lanacion.com.ar/tecnologia/feed/",            "category": "latam",    "lang": "es"},
        {"name": "Xataka",            "url": "https://feeds.weblogssl.com/xataka",                      "category": "latam",    "lang": "es"},
        {"name": "Genbeta",           "url": "https://feeds.weblogssl.com/genbeta",                     "category": "latam",    "lang": "es"},
    ]
}

_cache: dict = {}
CACHE_TTL = 300  # 5 minutes

VISITS_FILE = Path("visits.json")

def _load_visits() -> int:
    try:
        return json.loads(VISITS_FILE.read_text()).get("count", 0)
    except Exception:
        return 0

def _save_visits(count: int):
    try:
        VISITS_FILE.write_text(json.dumps({"count": count}))
    except Exception:
        pass

_visit_count: int = _load_visits()


def parse_date(entry) -> tuple[str, float]:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                dt = datetime(*val[:6])
                return dt.strftime("%d %b %Y, %H:%M"), dt.timestamp()
            except Exception:
                pass
    return "Fecha desconocida", 0.0


def extract_image(entry, category: str = "tech") -> str:
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url", "")
        if url:
            return url
    if hasattr(entry, "media_content") and entry.media_content:
        for m in entry.media_content:
            if m.get("medium") == "image" or "image" in m.get("type", ""):
                url = m.get("url", "")
                if url:
                    return url
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if "image" in enc.get("type", ""):
                url = enc.get("href") or enc.get("url") or ""
                if url:
                    return url
    return PLACEHOLDERS.get(category, PLACEHOLDERS["tech"])


async def fetch_feed(source: dict, client: httpx.AsyncClient) -> list[dict]:
    cache_key = source["url"]
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]

    try:
        resp = await client.get(source["url"], timeout=10, follow_redirects=True)
        feed = feedparser.parse(resp.text)
        articles = []
        for entry in feed.entries[:15]:
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            # Strip HTML tags simply
            import re
            clean_summary = re.sub(r"<[^>]+>", "", summary)[:300]
            date_str, date_ts = parse_date(entry)
            articles.append({
                "title": getattr(entry, "title", "Sin título"),
                "link": getattr(entry, "link", "#"),
                "summary": clean_summary,
                "date": date_str,
                "date_ts": date_ts,
                "source": source["name"],
                "category": source["category"],
                "lang": source.get("lang", "en"),
                "image": extract_image(entry, source["category"]),
            })
        _cache[cache_key] = {"ts": now, "data": articles}
        return articles
    except Exception:
        return []


async def get_all_news(category: str = "all", search: str = "") -> list[dict]:
    sources = RSS_SOURCES["all"]
    if category != "all":
        sources = [s for s in sources if s["category"] == category]

    async with httpx.AsyncClient(headers={"User-Agent": "TechPulse-NewsBot/1.0"}) as client:
        tasks = [fetch_feed(s, client) for s in sources]
        results = await asyncio.gather(*tasks)

    all_articles = [a for feed in results for a in feed]

    if search:
        query = search.lower()
        all_articles = [
            a for a in all_articles
            if query in a["title"].lower() or query in a["summary"].lower()
        ]

    all_articles.sort(key=lambda x: x["date_ts"], reverse=True)
    return all_articles


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    global _visit_count
    _visit_count += 1
    _save_visits(_visit_count)
    return templates.TemplateResponse("index.html", {"request": request, "visit_count": _visit_count})


@app.get("/api/stats")
async def api_stats():
    return JSONResponse({"visits": _visit_count})


@app.get("/api/news")
async def api_news(
    category: str = Query("all"),
    search: str = Query(""),
    page: int = Query(1),
    per_page: int = Query(12),
):
    articles = await get_all_news(category=category, search=search)
    total = len(articles)
    start = (page - 1) * per_page
    end = start + per_page
    return JSONResponse({
        "articles": articles[start:end],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })


@app.get("/api/sources")
async def api_sources():
    return [{"name": s["name"], "category": s["category"]} for s in RSS_SOURCES["all"]]


def _do_translate(text: str, limit: int = 500) -> str:
    key = f"{limit}:{text}"
    if key in _translate_cache:
        return _translate_cache[key]
    try:
        result = GoogleTranslator(source="auto", target="es").translate(text[:limit])
        _translate_cache[key] = result or text
        return _translate_cache[key]
    except Exception:
        return text


def _extract_article_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "figure"]):
        tag.decompose()
    # Try article body first, then fall back to all paragraphs
    container = soup.find("article") or soup.find(class_=lambda c: c and any(
        x in c for x in ("article", "post-body", "entry-content", "story", "content")
    )) or soup.body
    if not container:
        return ""
    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p") if len(p.get_text(strip=True)) > 60]
    return " ".join(paragraphs[:8])


@app.post("/api/translate")
async def api_translate(payload: dict):
    texts: list[str] = payload.get("texts", [])
    if not texts:
        return JSONResponse({"translations": []})
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(_executor, _do_translate, t) for t in texts]
    results = await asyncio.gather(*tasks)
    return JSONResponse({"translations": list(results)})


@app.get("/api/article-summary")
async def api_article_summary(url: str):
    cache_key = f"summary:{url}"
    if cache_key in _translate_cache:
        return JSONResponse({"summary": _translate_cache[cache_key]})

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; TechPulseBot/1.0)"},
            follow_redirects=True,
            timeout=10,
        ) as client:
            resp = await client.get(url)
            raw_text = _extract_article_text(resp.text)
    except Exception:
        return JSONResponse({"summary": ""})

    if not raw_text:
        return JSONResponse({"summary": ""})

    loop = asyncio.get_event_loop()
    translated = await loop.run_in_executor(_executor, lambda: _do_translate(raw_text, limit=2000))
    _translate_cache[cache_key] = translated
    return JSONResponse({"summary": translated})
