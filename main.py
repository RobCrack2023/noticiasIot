import asyncio
import base64
import json
import logging
import os
import secrets
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
import feedparser
import httpx
from deep_translator import GoogleTranslator
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pywebpush import webpush, WebPushException
from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("techpulse")

_translate_cache: dict[str, str] = {}
_executor = ThreadPoolExecutor(max_workers=4)

# ── Push notifications ─────────────────────────────────────────────────────────

VAPID_KEYS_FILE = Path("vapid_keys.json")
SUBSCRIPTIONS_FILE = Path("subscriptions.json")
VAPID_CLAIMS = {"sub": "mailto:admin@techpulse.cl"}


def _load_or_generate_vapid() -> tuple[str, str]:
    if VAPID_KEYS_FILE.exists():
        try:
            d = json.loads(VAPID_KEYS_FILE.read_text())
            return d["private_pem"], d["public_b64"]
        except Exception:
            pass
    private_key = generate_private_key(SECP256R1())
    private_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    ).decode()
    raw_pub = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode()
    VAPID_KEYS_FILE.write_text(json.dumps({"private_pem": private_pem, "public_b64": public_b64}))
    log.info("VAPID keys generated and saved.")
    return private_pem, public_b64


try:
    VAPID_PRIVATE_PEM, VAPID_PUBLIC_KEY = _load_or_generate_vapid()
    _push_enabled = True
except Exception as _e:
    log.warning("Push notifications disabled: %s", _e)
    VAPID_PRIVATE_PEM, VAPID_PUBLIC_KEY = "", ""
    _push_enabled = False

_subscriptions: list[dict] = []


def _load_subscriptions():
    global _subscriptions
    try:
        _subscriptions = json.loads(SUBSCRIPTIONS_FILE.read_text())
    except Exception:
        _subscriptions = []


def _save_subscriptions():
    try:
        SUBSCRIPTIONS_FILE.write_text(json.dumps(_subscriptions, indent=2))
    except Exception as e:
        log.warning("Could not save subscriptions: %s", e)


_load_subscriptions()


def _do_send_push(sub: dict, payload: str) -> bool:
    """Returns False if subscription is expired/invalid (should be removed)."""
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_PEM,
            vapid_claims=VAPID_CLAIMS,
        )
        return True
    except WebPushException as exc:
        if exc.response is not None and exc.response.status_code in (404, 410):
            return False
        log.warning("Push send error: %s", exc)
        return True
    except Exception as exc:
        log.warning("Push send error: %s", exc)
        return True


async def _send_push_notifications(articles: list[dict]):
    global _subscriptions
    if not articles or not _subscriptions or not _push_enabled:
        return
    count = len(articles)
    payload = json.dumps({
        "title": f"TechPulse — {count} noticia{'s' if count > 1 else ''} nueva{'s' if count > 1 else ''}",
        "body": articles[0]["title"][:120],
        "url": "/",
    })
    loop = asyncio.get_event_loop()
    dead: list[str] = []

    async def _one(sub: dict):
        ok = await loop.run_in_executor(_executor, lambda: _do_send_push(sub, payload))
        if not ok:
            dead.append(sub.get("endpoint", ""))

    await asyncio.gather(*[_one(s) for s in list(_subscriptions)])

    if dead:
        _subscriptions = [s for s in _subscriptions if s.get("endpoint") not in dead]
        _save_subscriptions()
        log.info("Removed %d expired push subscription(s).", len(dead))

# ── End push setup ─────────────────────────────────────────────────────────────

PLACEHOLDERS = {
    "tech":     "/static/img/placeholder-tech.svg",
    "iot":      "/static/img/placeholder-iot.svg",
    "robotics": "/static/img/placeholder-robotics.svg",
    "latam":    "/static/img/placeholder-latam.svg",
}
REFRESH_INTERVAL = 300  # seconds


_known_article_links: set[str] = set()


async def _prefetch_all():
    """Warm up the RSS cache and notify subscribers about new articles."""
    global _known_article_links
    log.info("Prefetching all feeds…")
    articles = await get_all_news(category="all")
    current_links = {a["link"] for a in articles}

    new_count = 0
    if _known_article_links:
        new_links = current_links - _known_article_links
        new_count = len(new_links)
        if new_links and _subscriptions:
            new_articles = sorted(
                [a for a in articles if a["link"] in new_links],
                key=lambda x: x["date_ts"], reverse=True,
            )
            asyncio.create_task(_send_push_notifications(new_articles))

    _known_article_links = current_links
    log.info("Feed cache warm. %d total, %d new.", len(current_links), new_count)


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

SOURCES_FILE = Path("sources.json")

def _load_sources() -> list[dict]:
    try:
        return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def _save_sources(sources: list[dict]):
    SOURCES_FILE.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")

RSS_SOURCES: dict[str, list[dict]] = {"all": _load_sources()}

# ── Admin auth ─────────────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(12)
    log.warning("ADMIN_PASSWORD no configurado. Usando: %s  (define la variable de entorno para fijarlo)", ADMIN_PASSWORD)

_admin_session: str | None = None

def _new_admin_session() -> str:
    global _admin_session
    _admin_session = secrets.token_urlsafe(32)
    return _admin_session

def _is_admin(request: Request) -> bool:
    token = request.cookies.get("admin_session", "")
    return bool(_admin_session and secrets.compare_digest(_admin_session, token))

def _require_admin_api(request: Request):
    if not _is_admin(request):
        raise __import__("fastapi").HTTPException(status_code=401, detail="No autorizado")

# ── End admin auth ─────────────────────────────────────────────────────────────

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


def _normalize(text: str) -> str:
    """Lowercase + strip diacritics so 'robotica' matches 'robótica'."""
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


def _matches_search(article: dict, query: str) -> bool:
    """All whitespace-separated terms must appear somewhere in title/summary/source."""
    haystack = _normalize(
        f"{article.get('title','')} {article.get('summary','')} {article.get('source','')}"
    )
    return all(term in haystack for term in _normalize(query).split() if term)


async def get_all_news(category: str = "all", search: str = "") -> list[dict]:
    sources = RSS_SOURCES["all"]
    if category != "all":
        sources = [s for s in sources if s["category"] == category]

    async with httpx.AsyncClient(headers={"User-Agent": "TechPulse-NewsBot/1.0"}) as client:
        tasks = [fetch_feed(s, client) for s in sources]
        results = await asyncio.gather(*tasks)

    all_articles = [a for feed in results for a in feed]

    if search:
        all_articles = [a for a in all_articles if _matches_search(a, search)]

    all_articles.sort(key=lambda x: x["date_ts"], reverse=True)
    return all_articles


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    global _visit_count
    is_new = not request.cookies.get("tp_visited")
    if is_new:
        _visit_count += 1
        _save_visits(_visit_count)
    response = templates.TemplateResponse("index.html", {"request": request, "visit_count": _visit_count})
    if is_new:
        response.set_cookie("tp_visited", "1", max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response


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


def _find_article_in_cache(article_url: str) -> dict | None:
    for feed_data in _cache.values():
        for article in feed_data.get("data", []):
            if article.get("link") == article_url:
                return article
    return None


@app.get("/article", response_class=HTMLResponse)
async def article_landing(
    request: Request,
    url: str = Query(...),
    t: str = Query(""),
    s: str = Query(""),
    c: str = Query("tech"),
):
    article = _find_article_in_cache(url)
    if not article:
        article = {
            "title": t or "Artículo",
            "summary": "",
            "source": s or "Fuente",
            "category": c or "tech",
            "date": "",
            "image": PLACEHOLDERS.get(c, PLACEHOLDERS["tech"]),
            "link": url,
            "lang": "es",
        }
    site_base = str(request.base_url).rstrip("/")
    image = article["image"]
    if image.startswith("/"):
        image = site_base + image
    return templates.TemplateResponse("article.html", {
        "request": request,
        "article": article,
        "landing_url": str(request.url),
        "og_image": image,
        "site_base": site_base,
    })


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    content = Path("static/sw.js").read_text()
    return Response(content=content, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/"})


@app.get("/api/push/vapid-key")
async def api_push_vapid_key():
    if not _push_enabled:
        return JSONResponse({"error": "push not available"}, status_code=503)
    return JSONResponse({"publicKey": VAPID_PUBLIC_KEY})


@app.post("/api/push/subscribe")
async def api_push_subscribe(payload: dict):
    if not _push_enabled:
        return JSONResponse({"error": "push not available"}, status_code=503)
    sub = payload.get("subscription")
    if not sub or not sub.get("endpoint"):
        return JSONResponse({"error": "invalid subscription"}, status_code=400)
    known_endpoints = {s.get("endpoint") for s in _subscriptions}
    if sub["endpoint"] not in known_endpoints:
        _subscriptions.append(sub)
        _save_subscriptions()
    return JSONResponse({"ok": True, "total": len(_subscriptions)})


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(payload: dict):
    global _subscriptions
    endpoint = payload.get("endpoint", "")
    _subscriptions = [s for s in _subscriptions if s.get("endpoint") != endpoint]
    _save_subscriptions()
    return JSONResponse({"ok": True})


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


# ── Admin routes ───────────────────────────────────────────────────────────────

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if _is_admin(request):
        return RedirectResponse("/admin")
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": False})

@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, ADMIN_PASSWORD):
        token = _new_admin_session()
        resp = RedirectResponse("/admin", status_code=302)
        resp.set_cookie("admin_session", token, httponly=True, samesite="strict", max_age=86400)
        return resp
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": True}, status_code=401)

@app.get("/admin/logout")
async def admin_logout():
    global _admin_session
    _admin_session = None
    resp = RedirectResponse("/admin/login", status_code=302)
    resp.delete_cookie("admin_session")
    return resp

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse("admin.html", {"request": request})

# ── Admin API ──────────────────────────────────────────────────────────────────

@app.get("/admin/api/stats")
async def admin_stats(request: Request):
    _require_admin_api(request)
    cached_articles = sum(len(v.get("data", [])) for v in _cache.values())
    return JSONResponse({
        "visits":          _visit_count,
        "sources":         len(RSS_SOURCES["all"]),
        "cached_articles": cached_articles,
        "push_subscribers": len(_subscriptions),
        "push_enabled":    _push_enabled,
    })

@app.get("/admin/api/sources")
async def admin_get_sources(request: Request):
    _require_admin_api(request)
    return JSONResponse(RSS_SOURCES["all"])

@app.post("/admin/api/sources")
async def admin_add_source(request: Request, payload: dict):
    _require_admin_api(request)
    name     = (payload.get("name") or "").strip()
    url      = (payload.get("url") or "").strip()
    category = payload.get("category", "tech")
    lang     = payload.get("lang", "en")
    if not name or not url:
        return JSONResponse({"error": "name y url son requeridos"}, status_code=400)
    if category not in ("tech", "iot", "robotics", "latam"):
        return JSONResponse({"error": "categoría inválida"}, status_code=400)
    if any(s["url"] == url for s in RSS_SOURCES["all"]):
        return JSONResponse({"error": "La URL ya existe"}, status_code=409)
    new_source = {"name": name, "url": url, "category": category, "lang": lang}
    RSS_SOURCES["all"].append(new_source)
    _save_sources(RSS_SOURCES["all"])
    return JSONResponse({"ok": True, "source": new_source})

@app.delete("/admin/api/sources")
async def admin_delete_source(request: Request, payload: dict):
    _require_admin_api(request)
    url = payload.get("url", "")
    before = len(RSS_SOURCES["all"])
    RSS_SOURCES["all"] = [s for s in RSS_SOURCES["all"] if s["url"] != url]
    if len(RSS_SOURCES["all"]) == before:
        return JSONResponse({"error": "Fuente no encontrada"}, status_code=404)
    _cache.pop(url, None)
    _save_sources(RSS_SOURCES["all"])
    return JSONResponse({"ok": True})

@app.post("/admin/api/sources/test")
async def admin_test_source(request: Request, payload: dict):
    _require_admin_api(request)
    url = (payload.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "URL vacía"})
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "TechPulse-AdminBot/1.0"},
            follow_redirects=True, timeout=10
        ) as client:
            resp = await client.get(url)
        feed = feedparser.parse(resp.text)
        if not feed.entries:
            return JSONResponse({"ok": False, "error": "Feed válido pero sin artículos"})
        return JSONResponse({
            "ok":       True,
            "title":    feed.feed.get("title", "Sin título"),
            "articles": len(feed.entries),
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})

@app.post("/admin/api/refresh")
async def admin_force_refresh(request: Request):
    _require_admin_api(request)
    _cache.clear()
    asyncio.create_task(_prefetch_all())
    return JSONResponse({"ok": True})

@app.post("/admin/api/visits/reset")
async def admin_reset_visits(request: Request):
    _require_admin_api(request)
    global _visit_count
    _visit_count = 0
    _save_visits(0)
    return JSONResponse({"ok": True})

@app.post("/admin/api/notify/test")
async def admin_notify_test(request: Request, payload: dict):
    _require_admin_api(request)
    if not _push_enabled or not _subscriptions:
        return JSONResponse({"ok": False, "error": "Sin suscriptores o push desactivado"})
    test_article = {
        "title": payload.get("title", "Notificación de prueba — TechPulse"),
        "date_ts": time.time(),
    }
    asyncio.create_task(_send_push_notifications([test_article]))
    return JSONResponse({"ok": True, "sent_to": len(_subscriptions)})
