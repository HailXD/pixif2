import asyncio
import io
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import aiohttp
import httpx
import numpy as np
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

TURSO_DB_URL = os.getenv("TURSO_DB_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN_WRITE", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
PHPSESSID = os.getenv("PHPSESSID", "")

IMG_BASE = "https://i.pximg.net/img-original/img/"


PIXIV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "referer": "https://www.pixiv.net/",
}
AI_TAGS = {
    "stablediffusion",
    "ai-generated",
    "novelai",
    "novelaidiffusionai",
    "aiart",
    "ai",
    "comfyui",
}
EXIF_TYPE_ORDER = ("novelai", "sd", "comfy", "mj", "celsys", "photoshop", "stealth")
EXIF_TYPE_TO_CODE = {name: idx + 1 for idx, name in enumerate(EXIF_TYPE_ORDER)}
POST_SCAN_LIMIT = 64
EXIF_RANGE_LIMIT = 96
FULL_IMAGE_LIMIT = 32
PAGE_SIZE = 60
SEARCH_PAGE_SIZE = 5
THUMB_MAX_AGE = 1800
THUMB_DIR = Path(tempfile.gettempdir()) / "pixif2-thumbs"
PAGE_URL_CACHE_MAX_AGE = 1800

app = FastAPI()
ACTIVE_TASKS = {}
TASK_EVENT_QUEUES = set()
PAGE_URL_CACHE = {}

FRONTEND_DIR = os.path.join(os.getcwd(), "frontend")


def base26_time():
    x = ""
    n = int(time.time() * 100)
    while n:
        x = chr(97 + n % 26) + x
        n //= 26
    return x


def base26_to_time(value):
    n = 0
    for c in value:
        if c < "a" or c > "z":
            return 0
        n = n * 26 + ord(c) - 97
    return n // 100


def turso_url():
    base = TURSO_DB_URL.rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    return base


async def turso_execute(stmts):
    url = turso_url() + "/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "requests": [{"type": "execute", "stmt": s} for s in stmts]
        + [{"type": "close"}]
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=body, headers=headers)
        r.raise_for_status()
        return r.json()


async def turso_batch(stmts):
    url = turso_url() + "/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "requests": [{"type": "execute", "stmt": s} for s in stmts]
        + [{"type": "close"}]
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(url, json=body, headers=headers)
        r.raise_for_status()
        return r.json()


async def init_db():
    await turso_execute(
        [
            {
                "sql": "CREATE TABLE IF NOT EXISTS pi_searches (id TEXT PRIMARY KEY, post_ids TEXT)"
            },
            {
                "sql": "CREATE TABLE IF NOT EXISTS pi_scans (post_id TEXT PRIMARY KEY, url TEXT, exif_type INTEGER)"
            },
        ]
    )
    for sql in (
        "DROP INDEX IF EXISTS pi_searches_created_at_idx",
        "DROP INDEX IF EXISTS pi_search_posts_search_pos_idx",
        "DROP INDEX IF EXISTS pi_search_posts_post_idx",
        "DROP TABLE IF EXISTS pi_search_posts",
        "ALTER TABLE pi_searches DROP COLUMN api_url",
        "ALTER TABLE pi_searches DROP COLUMN created_at",
    ):
        try:
            await turso_execute([{"sql": sql}])
        except httpx.HTTPStatusError as e:
            text = e.response.text.casefold()
            if "no such column" not in text and "no such index" not in text:
                raise


async def discord_notify(msg):
    if not DISCORD_WEBHOOK_URL:
        print("WARN: DISCORD_WEBHOOK_URL not set, skipping notify")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                DISCORD_WEBHOOK_URL.rstrip("/") + "/webhook-forward",
                json={"content": msg},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status >= 400:
                    body = await r.text()
                    print(f"Discord webhook failed ({r.status}): {body}")
    except Exception as e:
        print(f"Discord webhook error: {repr(e)}")


async def publish_task_event(search_id):
    data = {"id": search_id, "at": int(time.time())}
    for queue in list(TASK_EVENT_QUEUES):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(data)


async def finish_task(search_id):
    ACTIVE_TASKS.pop(search_id, None)
    await publish_task_event(search_id)


def is_ai_post(post):
    if post.get("aiType") == 2:
        return True
    tags = post.get("tags") or []
    for tag in tags:
        name = (
            tag
            if isinstance(tag, str)
            else (tag.get("tag") or tag.get("name") or "")
            if isinstance(tag, dict)
            else ""
        )
        if name and name.casefold() in AI_TAGS:
            return True
    return False


def get_search_keywords(raw):
    parts = urlsplit(raw)
    path_parts = [unquote(p) for p in parts.path.split("/") if p]
    if "tags" in path_parts:
        idx = path_parts.index("tags") + 1
        if idx < len(path_parts):
            return path_parts[idx]
    query = parse_qs(parts.query)
    words = query.get("word") or query.get("q")
    if words:
        return words[0]
    return raw.strip()


def get_search_params(raw, keywords):
    params = []
    for key, value in parse_qsl(urlsplit(raw).query, keep_blank_values=True):
        if key in ("p", "q", "word", "type"):
            continue
        if key == "s_mode":
            if value == "tag":
                value = "s_tag"
            elif value == "tag_full":
                value = "s_tag_full"
        params.append((key, value))
    params.append(("word", keywords))
    if not any(k == "s_mode" for k, _ in params):
        params.append(("s_mode", "s_tag"))
    return urlencode(params)


def get_search_api_url(raw, keywords):
    encoded = quote(keywords, safe="")
    params = get_search_params(raw, keywords)
    return f"https://www.pixiv.net/ajax/search/artworks/{encoded}?{params}"


async def save_search(search_id, post_ids):
    stmt = {
        "sql": "INSERT OR REPLACE INTO pi_searches (id, post_ids) VALUES (?, ?)",
        "args": [
            {"type": "text", "value": search_id},
            {"type": "text", "value": json.dumps(post_ids)},
        ],
    }
    await turso_execute([stmt])


async def pixiv_search_live(url, pages, mode, phpsessid, search_id):
    keywords = get_search_keywords(url)
    api_url = get_search_api_url(url, keywords)
    first_url = f"{api_url}&p=1"
    cookies = {"PHPSESSID": phpsessid}
    post_ids = []
    seen = set()
    done = 0
    async with aiohttp.ClientSession(cookies=cookies, headers=PIXIV_HEADERS) as session:
        tasks = [fetch_page(session, f"{api_url}&p={p}") for p in range(1, pages + 1)]
        for coro in asyncio.as_completed(tasks):
            data = await coro
            done += 1
            if search_id in ACTIVE_TASKS:
                ACTIVE_TASKS[search_id].update({"total": pages, "done": done})
            if data.get("error"):
                continue
            body = data.get("body") or {}
            posts = (body.get("illustManga") or {}).get("data") or []
            if mode == "ai":
                posts = [p for p in posts if is_ai_post(p)]
            elif mode == "real":
                posts = [p for p in posts if not is_ai_post(p)]
            for post in posts:
                pid = str(post.get("id") or "")
                if pid and pid not in seen:
                    seen.add(pid)
                    post_ids.append(pid)
            if done % 25 == 0:
                await save_search(search_id, post_ids)
    await save_search(search_id, post_ids)
    return post_ids, keywords, first_url


async def pixiv_user_posts(user_ids, phpsessid):
    cookies = {"PHPSESSID": phpsessid}
    results = []
    async with aiohttp.ClientSession(cookies=cookies, headers=PIXIV_HEADERS) as session:
        for uid in user_ids:
            data = await fetch_page(
                session, f"https://www.pixiv.net/ajax/user/{uid}/profile/all"
            )
            body = data.get("body") or {}
            posts = list((body.get("illusts") or {}).keys())
            username = ""
            pickup = body.get("pickup") or []
            if pickup:
                username = (pickup[0] or {}).get("userName") or ""
            if not username:
                udata = await fetch_page(
                    session, f"https://www.pixiv.net/ajax/user/{uid}"
                )
                username = (udata.get("body") or {}).get("name") or ""
            results.append({"user_id": uid, "post_ids": posts, "username": username})
    return results


async def fetch_page(session, url):
    async with session.get(url) as r:
        return await r.json()


async def get_post_pages(post_id, session):
    key = str(post_id)
    cached = PAGE_URL_CACHE.get(key)
    now = time.time()
    if cached and now - cached["time"] < PAGE_URL_CACHE_MAX_AGE:
        return cached["pages"]
    data = await fetch_page(session, f"https://www.pixiv.net/ajax/illust/{post_id}/pages")
    pages = []
    for page in data.get("body") or []:
        urls = page.get("urls") or {}
        pages.append(
            {
                "original": urls.get("original") or "",
                "regular": urls.get("regular") or "",
                "small": urls.get("small") or "",
                "thumb_mini": urls.get("thumb_mini") or "",
            }
        )
    PAGE_URL_CACHE[key] = {"time": now, "pages": pages}
    return pages


def parse_png_metadata(data):
    index = 8
    while index < len(data):
        if index + 8 > len(data):
            break
        chunk_len = int.from_bytes(data[index : index + 4], "big")
        chunk_type = data[index + 4 : index + 8].decode("ascii", errors="ignore")
        index += 8
        if chunk_type in ("tEXt", "iTXt"):
            content = data[index : index + chunk_len]
            return (
                content.replace(b"\0", b"") if chunk_type == "tEXt" else content.strip()
            )
        index += chunk_len + 4
    return None


def determine_exif_type(metadata):
    if metadata is None:
        return None
    if metadata == b"TitleAI generated image":
        return "novelai"
    if metadata.startswith(b"parameter"):
        return "sd"
    if b'{"' in metadata:
        return "comfy"
    if metadata.startswith(b"SoftwareCelsys"):
        return "celsys"
    return "photoshop"


def byteize(alpha):
    alpha = alpha.T.reshape((-1,))
    alpha = alpha[: (alpha.shape[0] // 8) * 8]
    alpha = np.bitwise_and(alpha, 1)
    alpha = alpha.reshape((-1, 8))
    return np.packbits(alpha, axis=1)


def has_stealth_png_bytes(data):
    try:
        image = Image.open(io.BytesIO(data))
        if "A" not in image.getbands():
            return False
        alpha = np.array(image.getchannel("A"))
        arr = byteize(alpha).flatten()
        magic = b"stealth_pngcomp"
        return bytes(arr[: len(magic)]) == magic
    except Exception:
        return False


async def scan_post(post_id, session, post_sem, exif_sem, img_sem):
    async with post_sem:
        try:
            pages = await get_post_pages(post_id, session)
            image_urls = [p["original"] for p in pages if "png" in p["original"]]
            for url in image_urls:
                metadata = await get_exif_range(url, session, exif_sem)
                exif_type = determine_exif_type(metadata)
                if exif_type not in ("photoshop", "celsys", None):
                    code = EXIF_TYPE_TO_CODE.get(exif_type)
                    return post_id, url, code
            for url in image_urls:
                img_data = await fetch_image(session, url, img_sem)
                if img_data and has_stealth_png_bytes(img_data):
                    return post_id, url, EXIF_TYPE_TO_CODE.get("stealth")
            return post_id, None, None
        except Exception:
            return post_id, None, None


async def get_exif_range(url, session, sem):
    hdrs = {"Referer": "https://www.pixiv.net/", "Range": "bytes=0-512"}
    if sem:
        async with sem:
            async with session.get(url, headers=hdrs) as r:
                data = await r.read()
    else:
        async with session.get(url, headers=hdrs) as r:
            data = await r.read()
    return parse_png_metadata(data)


async def fetch_image(session, url, sem):
    if sem:
        async with sem:
            async with session.get(url) as r:
                return await r.read()
    async with session.get(url) as r:
        return await r.read()


async def run_scan(post_ids, phpsessid, task_id=None, save_live=False):
    post_sem = asyncio.Semaphore(POST_SCAN_LIMIT)
    exif_sem = asyncio.Semaphore(EXIF_RANGE_LIMIT)
    img_sem = asyncio.Semaphore(FULL_IMAGE_LIMIT)
    cookies = {"PHPSESSID": phpsessid}
    results = []
    async with aiohttp.ClientSession(cookies=cookies, headers=PIXIV_HEADERS) as session:
        tasks = [
            scan_post(pid, session, post_sem, exif_sem, img_sem) for pid in post_ids
        ]
        pending = []
        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)
            pending.append(result)
            if task_id and task_id in ACTIVE_TASKS:
                ACTIVE_TASKS[task_id]["done"] = len(results)
            if save_live and len(pending) >= 20:
                await save_scan_results(pending)
                pending = []
        if save_live and pending:
            await save_scan_results(pending)
    return results


async def save_scan_results(results):
    stmts = []
    for post_id, url, exif_type in results:
        short_url = url.replace(IMG_BASE, "", 1) if url else ""
        stmts.append(
            {
                "sql": "INSERT OR REPLACE INTO pi_scans (post_id, url, exif_type) VALUES (?, ?, ?)",
                "args": [
                    {"type": "text", "value": str(post_id)},
                    {"type": "text", "value": short_url},
                    {"type": "integer", "value": str(exif_type)}
                    if exif_type
                    else {"type": "null"},
                ],
            }
        )
    if stmts:
        for i in range(0, len(stmts), 200):
            await turso_batch(stmts[i : i + 200])


async def get_scanned_post_ids(post_ids):
    if not post_ids:
        return {}
    chunks = [post_ids[i : i + 500] for i in range(0, len(post_ids), 500)]
    scanned = {}
    stmts = []
    for chunk in chunks:
        placeholders = ",".join("?" for _ in chunk)
        stmts.append(
            {
                "sql": f"SELECT post_id, url, exif_type FROM pi_scans WHERE post_id IN ({placeholders})",
                "args": [{"type": "text", "value": str(pid)} for pid in chunk],
            }
        )
    resp = await turso_execute(stmts)
    for result in resp.get("results") or []:
        if "response" not in result:
            continue
        rows = result["response"].get("result", {}).get("rows", [])
        for row in rows:
            pid = row[0].get("value")
            url_val = row[1].get("value") if row[1].get("type") != "null" else ""
            et = row[2].get("value") if row[2].get("type") != "null" else None
            scanned[pid] = {"url": url_val, "exif_type": int(et) if et else None}
    return scanned


def exif_items(post_ids, scanned):
    items = []
    for pid in post_ids:
        s = scanned.get(pid)
        if not s or not s.get("exif_type"):
            continue
        items.append(
            {
                "post_id": pid,
                "url": s.get("url"),
                "exif_type": s.get("exif_type"),
                "scanned": True,
                **image_links(pid, s.get("url")),
            }
        )
    return items


def cleanup_thumbs():
    THUMB_DIR.mkdir(exist_ok=True)
    now = time.time()
    for path in THUMB_DIR.glob("*.webp"):
        try:
            if now - path.stat().st_atime > THUMB_MAX_AGE:
                path.unlink()
        except OSError:
            pass


def page_num_from_url(url):
    if not url:
        return 0
    name = url.rsplit("/", 1)[-1]
    if "_p" not in name:
        return 0
    try:
        return int(name.rsplit("_p", 1)[1].split(".", 1)[0])
    except ValueError:
        return 0


def media_type_from_url(url):
    ext = urlsplit(url).path.rsplit(".", 1)[-1].casefold()
    if ext in ("jpg", "jpeg"):
        return "image/jpeg"
    if ext == "png":
        return "image/png"
    if ext == "gif":
        return "image/gif"
    if ext == "webp":
        return "image/webp"
    return "application/octet-stream"


async def get_pixiv_image_url(post_id, page, size, phpsessid):
    cookies = {"PHPSESSID": phpsessid}
    async with aiohttp.ClientSession(cookies=cookies, headers=PIXIV_HEADERS) as session:
        pages = await get_post_pages(post_id, session)
    if not pages:
        raise HTTPException(status_code=404, detail="image not found")
    page = min(max(page, 0), len(pages) - 1)
    urls = pages[page]
    if size in ("full", "orig"):
        url = urls.get("original") or urls.get("regular") or urls.get("small")
    else:
        url = urls.get("regular") or urls.get("small") or urls.get("original")
    if not url:
        raise HTTPException(status_code=404, detail="image not found")
    return url


async def fetch_pixiv_bytes(url, phpsessid):
    cookies = {"PHPSESSID": phpsessid}
    async with aiohttp.ClientSession(cookies=cookies, headers=PIXIV_HEADERS) as session:
        async with session.get(url) as r:
            if r.status >= 400:
                raise HTTPException(status_code=r.status, detail="image fetch failed")
            return await r.read(), r.headers.get("Content-Type") or media_type_from_url(url)


async def create_webp(post_id, image_url, phpsessid, page=0, kind="t"):
    cleanup_thumbs()
    out = THUMB_DIR / f"{post_id}_p{page}_{kind}.webp"
    if out.exists():
        os.utime(out, None)
        return out
    data, _ = await fetch_pixiv_bytes(image_url, phpsessid)
    if not data:
        raise HTTPException(status_code=404, detail="image not found")
    image = Image.open(io.BytesIO(data))
    if kind == "v":
        image = image.resize((max(image.width // 2, 1), max(image.height // 2, 1)))
    else:
        image = image.resize((max(image.width // 3, 1), max(image.height // 3, 1)))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    image.save(out, "WEBP", quality=82 if kind == "v" else 72)
    return out


def image_links(post_id, url):
    page = page_num_from_url(url)
    suffix = f"?p={page}"
    pid = quote(str(post_id), safe="")
    return {
        "image_url": f"/api/i/{pid}/t{suffix}",
        "preview_url": f"/api/i/{pid}/v{suffix}",
        "download_url": f"/api/i/{pid}/o{suffix}",
        "full_image_url": f"/api/i/{pid}/v{suffix}",
        "page": page,
    }


async def bg_search_task(search_id, url, pages, mode, phpsessid):
    ACTIVE_TASKS[search_id] = {
        "type": "search",
        "phase": "searching",
        "total": pages,
        "done": 0,
    }
    await discord_notify(f"`{search_id}` started")
    try:
        post_ids, _, _ = await pixiv_search_live(url, pages, mode, phpsessid, search_id)
        await discord_notify(f"`{search_id}` completed - {len(post_ids)} posts found")
    except Exception as e:
        await discord_notify(f"`{search_id}` failed: {e}")
    finally:
        await finish_task(search_id)


async def bg_user_task(search_id, user_ids, phpsessid):
    ACTIVE_TASKS[search_id] = {
        "type": "user_search",
        "phase": "searching",
        "total": len(user_ids),
        "done": 0,
    }
    await discord_notify(f"`{search_id}` started (users)")
    try:
        results = await pixiv_user_posts(user_ids, phpsessid)
        all_post_ids = []
        for r in results:
            all_post_ids.extend(r["post_ids"])
        all_post_ids = list(dict.fromkeys(all_post_ids))
        await save_search(search_id, all_post_ids)
        already = await get_scanned_post_ids(all_post_ids)
        to_scan = [pid for pid in all_post_ids if pid not in already]
        if to_scan:
            ACTIVE_TASKS[search_id].update(
                {"phase": "scanning", "total": len(to_scan), "done": 0}
            )
            await run_scan(to_scan, phpsessid, task_id=search_id, save_live=True)
        await discord_notify(
            f"`{search_id}` completed - {len(all_post_ids)} posts from {len(user_ids)} users"
        )
    except Exception as e:
        await discord_notify(f"`{search_id}` failed: {e}")
    finally:
        await finish_task(search_id)


async def bg_scan_task(search_id, post_ids, phpsessid):
    ACTIVE_TASKS[search_id] = {
        "type": "scan",
        "phase": "scanning",
        "total": len(post_ids),
        "done": 0,
    }
    await discord_notify(f"`{search_id}` scan started ({len(post_ids)} posts)")
    try:
        results = await run_scan(post_ids, phpsessid, task_id=search_id, save_live=True)
        found = sum(1 for _, url, _ in results if url)
        await discord_notify(
            f"`{search_id}` scan completed - {found}/{len(post_ids)} have exif"
        )
    except Exception as e:
        await discord_notify(f"`{search_id}` scan failed: {e}")
    finally:
        await finish_task(search_id)


async def bg_search_and_scan_task(search_id, url, pages, mode, phpsessid):
    ACTIVE_TASKS[search_id] = {
        "type": "search+scan",
        "phase": "searching",
        "total": pages,
        "done": 0,
    }
    await discord_notify(f"`{search_id}` search+scan started")
    try:
        post_ids, _, _ = await pixiv_search_live(url, pages, mode, phpsessid, search_id)
        await discord_notify(
            f"`{search_id}` search done - {len(post_ids)} posts, scanning..."
        )
        already = await get_scanned_post_ids(post_ids)
        to_scan = [pid for pid in post_ids if pid not in already]
        if to_scan:
            ACTIVE_TASKS[search_id].update(
                {"phase": "scanning", "total": len(to_scan), "done": 0}
            )
            results = await run_scan(
                to_scan, phpsessid, task_id=search_id, save_live=True
            )
            found = sum(1 for _, url, _ in results if url)
            await discord_notify(
                f"`{search_id}` scan completed - {found}/{len(to_scan)} new exif"
            )
        else:
            await discord_notify(f"`{search_id}` all {len(post_ids)} already scanned")
    except Exception as e:
        await discord_notify(f"`{search_id}` failed: {e}")
    finally:
        await finish_task(search_id)


class SearchRequest(BaseModel):
    url: str
    pages: int = 30
    mode: str = "ai"
    action: str = "search"


class UserSearchRequest(BaseModel):
    user_ids: list
    action: str = "search"


class ScanRequest(BaseModel):
    search_id: str


class RenameRequest(BaseModel):
    new_id: str


@app.on_event("startup")
async def startup():
    if not TURSO_DB_URL:
        print("WARN: TURSO_DB_URL not set, skipping DB init")
        return
    try:
        await init_db()
    except Exception as e:
        print(f"WARN: DB init failed ({e}), will retry on first request")


@app.post("/api/submit")
async def submit_search(req: SearchRequest, bg: BackgroundTasks):
    search_id = base26_time()
    phpsessid = PHPSESSID
    bg.add_task(
        bg_search_and_scan_task, search_id, req.url, req.pages, req.mode, phpsessid
    )
    return {"id": search_id, "status": "started"}


@app.post("/api/submit_users")
async def submit_users(req: UserSearchRequest, bg: BackgroundTasks):
    search_id = base26_time()
    phpsessid = PHPSESSID
    user_ids = [int(u) for u in req.user_ids]
    bg.add_task(bg_user_task, search_id, user_ids, phpsessid)
    return {"id": search_id, "status": "started"}


@app.post("/api/scan")
async def scan_search(req: ScanRequest, bg: BackgroundTasks):
    phpsessid = PHPSESSID
    if req.search_id in ACTIVE_TASKS:
        return {"status": "active", **ACTIVE_TASKS[req.search_id]}
    resp = await turso_execute(
        [
            {
                "sql": "SELECT post_ids FROM pi_searches WHERE id = ?",
                "args": [{"type": "text", "value": req.search_id}],
            }
        ]
    )
    results = resp.get("results") or []
    if not results or "response" not in results[0]:
        return {"error": "not found"}
    rows = results[0]["response"].get("result", {}).get("rows", [])
    if not rows:
        return {"error": "not found"}
    post_ids = json.loads(rows[0][0].get("value", "[]"))
    already = await get_scanned_post_ids(post_ids)
    to_scan = [pid for pid in post_ids if pid not in already]
    if not to_scan:
        return {"status": "already_scanned", "count": len(post_ids)}
    bg.add_task(bg_scan_task, req.search_id, to_scan, phpsessid)
    return {"status": "scanning", "total": len(post_ids), "to_scan": len(to_scan)}


@app.get("/api/searches")
async def list_searches(page: int = 1):
    page = max(page, 1)
    offset = (page - 1) * SEARCH_PAGE_SIZE
    resp = await turso_execute(
        [
            {
                "sql": "SELECT COUNT(*) FROM pi_searches"
            },
            {
                "sql": "SELECT id FROM pi_searches ORDER BY id DESC LIMIT ? OFFSET ?",
                "args": [
                    {"type": "integer", "value": str(SEARCH_PAGE_SIZE)},
                    {"type": "integer", "value": str(offset)},
                ],
            },
        ]
    )
    results = resp.get("results") or []
    if len(results) < 2 or "response" not in results[1]:
        return {"items": [], "total": 0, "page": page, "pages": 1}
    count_rows = results[0].get("response", {}).get("result", {}).get("rows", [])
    total = int(count_rows[0][0].get("value", "0")) if count_rows else 0
    rows = results[1]["response"].get("result", {}).get("rows", [])
    items = []
    for row in rows:
        search_id = row[0].get("value")
        items.append(
            {
                "id": search_id,
                "created_at": base26_to_time(search_id),
            }
        )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": SEARCH_PAGE_SIZE,
        "pages": max((total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE, 1),
    }


@app.get("/api/search/{search_id}")
async def get_search(search_id: str):
    resp = await turso_execute(
        [
            {
                "sql": "SELECT id, post_ids FROM pi_searches WHERE id = ?",
                "args": [{"type": "text", "value": search_id}],
            }
        ]
    )
    results = resp.get("results") or []
    if not results or "response" not in results[0]:
        return {"error": "not found"}
    rows = results[0]["response"].get("result", {}).get("rows", [])
    if not rows:
        return {"error": "not found"}
    row = rows[0]
    post_ids = json.loads(row[1].get("value", "[]"))
    scanned = await get_scanned_post_ids(post_ids)
    return {
        "id": row[0].get("value"),
        "post_ids": post_ids,
        "created_at": base26_to_time(row[0].get("value")),
        "scanned": scanned,
    }


@app.get("/api/results/{search_id}")
async def get_results(search_id: str, page: int = 1, exif_only: bool = True):
    resp = await turso_execute(
        [
            {
                "sql": "SELECT post_ids FROM pi_searches WHERE id = ?",
                "args": [{"type": "text", "value": search_id}],
            }
        ]
    )
    results = resp.get("results") or []
    if not results or "response" not in results[0]:
        return {"error": "not found"}
    rows = results[0]["response"].get("result", {}).get("rows", [])
    if not rows:
        return {"error": "not found"}
    post_ids = json.loads(rows[0][0].get("value", "[]"))
    scanned = await get_scanned_post_ids(post_ids)
    source = (
        exif_items(post_ids, scanned)
        if exif_only
        else [
            {
                "post_id": pid,
                "url": scanned[pid]["url"] if pid in scanned else None,
                "exif_type": scanned[pid]["exif_type"] if pid in scanned else None,
                "scanned": pid in scanned,
                **image_links(pid, scanned[pid]["url"] if pid in scanned else ""),
            }
            for pid in post_ids
        ]
    )
    page = max(page, 1)
    total = len(source)
    start = (page - 1) * PAGE_SIZE
    items = source[start : start + PAGE_SIZE]
    return {
        "search_id": search_id,
        "items": items,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        "raw_total": len(post_ids),
        "scanned_count": len(scanned),
    }


@app.get("/api/i/{post_id}/t")
async def get_image_thumb(post_id: str, p=0):
    p = int(p or 0)
    image_url = await get_pixiv_image_url(post_id, p, "thumb", PHPSESSID)
    path = await create_webp(post_id, image_url, PHPSESSID, p, "t")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": f"public, max-age={THUMB_MAX_AGE}"},
    )


@app.get("/api/i/{post_id}/v")
async def get_image_preview(post_id: str, p=0):
    p = int(p or 0)
    image_url = await get_pixiv_image_url(post_id, p, "full", PHPSESSID)
    path = await create_webp(post_id, image_url, PHPSESSID, p, "v")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": f"public, max-age={THUMB_MAX_AGE}"},
    )


@app.get("/api/i/{post_id}/o")
async def get_image_original(post_id: str, p=0):
    p = int(p or 0)
    image_url = await get_pixiv_image_url(post_id, p, "orig", PHPSESSID)
    data, content_type = await fetch_pixiv_bytes(image_url, PHPSESSID)
    filename = urlsplit(image_url).path.rsplit("/", 1)[-1] or f"{post_id}_p{p}.png"
    return Response(
        data,
        media_type=content_type,
        headers={
            "Cache-Control": f"public, max-age={THUMB_MAX_AGE}",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.get("/api/image/{post_id}/thumb")
async def get_long_image_thumb(post_id: str, page: int = 0, p=None):
    return await get_image_thumb(post_id, page if p is None else p)


@app.get("/api/image/{post_id}/full")
async def get_long_image_full(post_id: str, page: int = 0, p=None):
    return await get_image_preview(post_id, page if p is None else p)


@app.get("/api/thumb/{post_id}")
async def get_thumb(post_id: str):
    return await get_image_thumb(post_id)


@app.delete("/api/search/{search_id}")
async def delete_search(search_id: str):
    await turso_execute(
        [
            {
                "sql": "DELETE FROM pi_searches WHERE id = ?",
                "args": [{"type": "text", "value": search_id}],
            }
        ]
    )
    return {"status": "deleted"}


@app.patch("/api/search/{search_id}")
async def rename_search(search_id: str, req: RenameRequest):
    resp = await turso_execute(
        [
            {
                "sql": "SELECT post_ids FROM pi_searches WHERE id = ?",
                "args": [{"type": "text", "value": search_id}],
            }
        ]
    )
    results = resp.get("results") or []
    if not results or "response" not in results[0]:
        return {"error": "not found"}
    rows = results[0]["response"].get("result", {}).get("rows", [])
    if not rows:
        return {"error": "not found"}
    post_ids_val = rows[0][0].get("value", "[]")
    await turso_execute(
        [
            {
                "sql": "DELETE FROM pi_searches WHERE id = ?",
                "args": [{"type": "text", "value": search_id}],
            },
            {
                "sql": "INSERT INTO pi_searches (id, post_ids) VALUES (?, ?)",
                "args": [
                    {"type": "text", "value": req.new_id},
                    {"type": "text", "value": post_ids_val},
                ],
            },
        ]
    )
    return {"status": "renamed", "new_id": req.new_id}


@app.get("/api/progress")
async def get_progress():
    return [{"id": k, **v} for k, v in ACTIVE_TASKS.items()]


@app.get("/api/events")
async def events(request: Request):
    queue = asyncio.Queue(maxsize=16)
    TASK_EVENT_QUEUES.add(queue)

    async def stream():
        try:
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            TASK_EVENT_QUEUES.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
