import os
import sys
import json
import base64
import urllib.parse
import re
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from bs4 import BeautifulSoup

# Try to use curl_cffi for Cloudflare TLS bypass, fallback to requests
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests
    HAS_CURL_CFFI = False

# Load configuration
CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    default_config = {
        "mirror": "https://rezka.ag",
        "port": 3000,
        "host": "0.0.0.0",
        "cookies": "",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_config.update(saved)
        except Exception as e:
            print(f"Error reading config: {e}")
    return default_config

def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving config: {e}")

config = load_config()

app = FastAPI(title="HDRezka Tizen Proxy Server", version="1.0.0")

# Enable CORS for all origins (so Samsung Tizen app can request without CORS errors)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TRASH_LIST = [
    '$$!!@$$@^!@#$$@', 
    '@@@@@!##!^^^', 
    '####^!!##!@@', 
    '^^^!@##!!##', 
    '$$#!!@#!@##'
]

def decode_trash(data: str) -> str:
    """Decode obfuscated HDRezka payload (starts with #h or trash tokens)"""
    if not data or not isinstance(data, str):
        return ""
    if not data.startswith("#"):
        return data

    x = data[2:]  # Remove leading '#h'
    for t in TRASH_LIST:
        enc_token = base64.b64encode(t.encode('utf-8')).decode('utf-8')
        token = f"//_//{enc_token}"
        x = x.replace(token, "")

    try:
        padding = 4 - (len(x) % 4)
        if padding < 4:
            x += "=" * padding
        decoded_bytes = base64.b64decode(x)
        return urllib.parse.unquote(decoded_bytes.decode('utf-8', errors='ignore'))
    except Exception as e:
        print(f"Error decoding payload: {e}")
        try:
            return base64.b64decode(x).decode('utf-8', errors='ignore')
        except Exception:
            return ""

def parse_playlist(stream_str: str) -> List[Dict[str, Any]]:
    """Parse comma-separated quality strings like [1080p Ultra]https://... or https://..."""
    playlists = []
    if not stream_str:
        return playlists

    matches = re.findall(r'\[(.*?)\]([^,\[]+)', stream_str)
    for quality, raw_urls in matches:
        raw_urls = raw_urls.strip()
        m3u8_url = None
        mp4_url = None

        if " or " in raw_urls:
            parts = raw_urls.split(" or ")
            for p in parts:
                clean_p = p.split(":hls:")[0].strip()
                if ".m3u8" in clean_p:
                    m3u8_url = clean_p
                elif ".mp4" in clean_p:
                    mp4_url = clean_p
                elif not m3u8_url:
                    m3u8_url = clean_p
        else:
            clean_p = raw_urls.split(":hls:")[0].strip()
            if ".m3u8" in clean_p:
                m3u8_url = clean_p
            else:
                mp4_url = clean_p

        primary = m3u8_url or mp4_url or raw_urls
        playlists.append({
            "quality": quality.strip(),
            "url": primary,
            "m3u8": m3u8_url or primary,
            "mp4": mp4_url
        })
    return playlists

def parse_subtitles(subtitle_str: str) -> List[Dict[str, str]]:
    subs = []
    if not subtitle_str:
        return subs
    matches = re.findall(r'\[(.*?)\](https?://[^\s,]+)', subtitle_str)
    for lang, url in matches:
        subs.append({
            "label": lang.strip(),
            "url": url.strip()
        })
    return subs

def get_session():
    """Returns requests session configured with headers, cookies, and TLS impersonation if available"""
    headers = {
        "User-Agent": config["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": config["mirror"].rstrip("/") + "/",
        "Origin": config["mirror"].rstrip("/"),
    }
    if HAS_CURL_CFFI:
        session = cffi_requests.Session(impersonate="chrome120")
    else:
        import requests
        session = requests.Session()
    
    session.headers.update(headers)

    if config.get("cookies"):
        # Parse cookies string "key=val; key2=val2"
        cookie_dict = {}
        for c in config["cookies"].split(";"):
            c = c.strip()
            if "=" in c:
                k, v = c.split("=", 1)
                cookie_dict[k.strip()] = v.strip()
        session.cookies.update(cookie_dict)

    return session

def fix_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return config["mirror"].rstrip("/") + url
    return url

# ==================== ENDPOINTS ====================

@app.get("/api/status")
def get_status():
    return {
        "status": "online",
        "mirror": config["mirror"],
        "tls_bypass": HAS_CURL_CFFI,
        "has_cookies": bool(config.get("cookies"))
    }

class ConfigModel(BaseModel):
    mirror: Optional[str] = None
    cookies: Optional[str] = None

@app.post("/api/config")
def update_config(payload: ConfigModel):
    global config
    if payload.mirror:
        config["mirror"] = payload.mirror.rstrip("/")
    if payload.cookies is not None:
        config["cookies"] = payload.cookies
    save_config(config)
    return {"success": True, "config": config}

@app.get("/api/catalog")
def get_catalog(
    category: Optional[str] = Query(None, description="films, series, cartoons, animation, or none"),
    filter_type: Optional[str] = Query("popular", description="popular, watching, new, last"),
    page: int = Query(1, ge=1)
):
    """Fetch movie/series catalog cards from HDRezka"""
    mirror = config["mirror"].rstrip("/")
    path = ""
    if category in ["films", "series", "cartoons", "animation"]:
        path = f"/{category}/"
        if filter_type and filter_type != "popular":
            path += f"{filter_type}/"
    
    if page > 1:
        path = f"{path}page/{page}/" if path.endswith("/") else f"{path}/page/{page}/"
    elif not path:
        path = "/"

    target_url = mirror + path
    session = get_session()

    try:
        resp = session.get(target_url, timeout=12)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Mirror returned {resp.status_code}")
        
        soup = BeautifulSoup(resp.text, "html.parser")
        items = []

        # Check for bot challenge
        if "Проверяем, что вы не бот!" in soup.text or "challenge-running" in resp.text:
            return JSONResponse(
                status_code=403,
                content={"error": "CLOUDFLARE_BLOCKED", "message": "Зеркало требует прохождения проверки на бота. Укажите персональное зеркало в настройках."}
            )

        cards = soup.select(".b-content__inline_item")
        for card in cards:
            link_el = card.select_one(".b-content__inline_item-link a")
            cover_el = card.select_one(".b-content__inline_item-cover img")
            info_el = card.select_one(".b-content__inline_item-link div")
            entity_el = card.select_one(".entity")

            if not link_el:
                continue

            href = link_el.get("href", "")
            title = link_el.text.strip()
            poster = fix_url(cover_el.get("src", "")) if cover_el else ""
            info = info_el.text.strip() if info_el else ""
            entity = entity_el.text.strip() if entity_el else ""
            
            # Extract ID from data-id or url
            post_id = card.get("data-id")
            if not post_id:
                m = re.search(r'/(\d+)-[^\/]+\.html', href)
                post_id = m.group(1) if m else href

            items.append({
                "id": str(post_id),
                "url": href,
                "title": title,
                "poster": poster,
                "info": info,
                "entity": entity,
                "is_series": "series" in href or "Сезон" in info or "сезон" in info
            })

        return {
            "page": page,
            "category": category,
            "items": items,
            "count": len(items)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/search")
def search_items(q: str = Query(..., min_length=1), page: int = Query(1, ge=1)):
    """Search titles on HDRezka"""
    mirror = config["mirror"].rstrip("/")
    search_url = f"{mirror}/search/?do=search&subaction=search&q={urllib.parse.quote(q)}"
    if page > 1:
        search_url += f"&page={page}"

    session = get_session()
    try:
        resp = session.get(search_url, timeout=12)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Mirror returned {resp.status_code}")

        soup = BeautifulSoup(resp.text, "html.parser")
        if "Проверяем, что вы не бот!" in soup.text:
            return JSONResponse(
                status_code=403,
                content={"error": "CLOUDFLARE_BLOCKED", "message": "Зеркало заблокировано проверкой. Используйте личное зеркало."}
            )

        items = []
        cards = soup.select(".b-content__inline_item")
        for card in cards:
            link_el = card.select_one(".b-content__inline_item-link a")
            cover_el = card.select_one(".b-content__inline_item-cover img")
            info_el = card.select_one(".b-content__inline_item-link div")
            entity_el = card.select_one(".entity")

            if not link_el:
                continue

            href = link_el.get("href", "")
            title = link_el.text.strip()
            poster = fix_url(cover_el.get("src", "")) if cover_el else ""
            info = info_el.text.strip() if info_el else ""
            entity = entity_el.text.strip() if entity_el else ""

            post_id = card.get("data-id")
            if not post_id:
                m = re.search(r'/(\d+)-[^\/]+\.html', href)
                post_id = m.group(1) if m else href

            items.append({
                "id": str(post_id),
                "url": href,
                "title": title,
                "poster": poster,
                "info": info,
                "entity": entity,
                "is_series": "series" in href or "Сезон" in info or "сезон" in info
            })

        return {"query": q, "page": page, "items": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/details")
def get_details(url: str = Query(...)):
    """Fetch movie/series details: title, poster, translators, ratings, plot, initial episodes"""
    target_url = fix_url(url)
    session = get_session()

    try:
        resp = session.get(target_url, timeout=12)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Mirror returned {resp.status_code}")

        soup = BeautifulSoup(resp.text, "html.parser")
        
        post_id_el = soup.select_one("input#post_id") or soup.select_one("[data-post_id]")
        post_id = post_id_el.get("value") if post_id_el and post_id_el.get("value") else ""
        if not post_id:
            m = re.search(r'/(\d+)-[^\/]+\.html', target_url)
            post_id = m.group(1) if m else ""

        title_el = soup.select_one("h1[itemprop='name']")
        title = title_el.text.strip() if title_el else ""

        orig_title_el = soup.select_one(".b-post__origtitle")
        orig_title = orig_title_el.text.strip() if orig_title_el else ""

        poster_el = soup.select_one(".b-sidecover img")
        poster = fix_url(poster_el.get("src", "")) if poster_el else ""

        desc_el = soup.select_one(".b-post__description_text")
        description = desc_el.text.strip() if desc_el else ""

        # Rating
        rating_imdb = soup.select_one(".b-post__info_rates .imdb .bold")
        rating_kp = soup.select_one(".b-post__info_rates .kp .bold")
        imdb_val = rating_imdb.text.strip() if rating_imdb else ""
        kp_val = rating_kp.text.strip() if rating_kp else ""

        # Info table (year, country, director, genre)
        info_dict = {}
        for row in soup.select(".b-post__info tr"):
            tds = row.select("td")
            if len(tds) == 2:
                key = tds[0].text.strip().replace(":", "")
                val = tds[1].text.strip()
                info_dict[key] = val

        # Translators / Dubbing studios
        translators = []
        trans_list = soup.select("#translators-list .b-translator__item")
        for item in trans_list:
            tr_id = item.get("data-translator_id", "")
            tr_title = item.get("title") or item.text.strip()
            is_active = "active" in item.get("class", [])
            translators.append({
                "id": str(tr_id),
                "name": tr_title,
                "active": is_active
            })

        # Check if series
        seasons_tab = soup.select("#simple-seasons-tabs .b-simple_season__item")
        is_series = bool(seasons_tab) or "/series/" in target_url or "/animation/" in target_url

        # Initial seasons and episodes if embedded in page
        seasons_data = []
        if seasons_tab:
            for s_el in seasons_tab:
                s_id = s_el.get("data-tab_sub_id")
                s_title = s_el.text.strip()
                
                # Find episodes matching this season
                episodes = []
                ep_els = soup.select(f"#simple-episodes-tabs .b-simple_episode__item[data-season_id='{s_id}']")
                for ep in ep_els:
                    ep_id = ep.get("data-episode_id")
                    ep_title = ep.text.strip()
                    episodes.append({
                        "season_id": int(s_id) if s_id.isdigit() else s_id,
                        "episode_id": int(ep_id) if ep_id.isdigit() else ep_id,
                        "title": ep_title
                    })

                seasons_data.append({
                    "season_id": int(s_id) if s_id.isdigit() else s_id,
                    "title": s_title,
                    "episodes": episodes
                })

        return {
            "id": str(post_id),
            "url": target_url,
            "title": title,
            "orig_title": orig_title,
            "poster": poster,
            "description": description,
            "imdb": imdb_val,
            "kp": kp_val,
            "info": info_dict,
            "is_series": is_series,
            "translators": translators,
            "seasons": seasons_data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/episodes")
def get_episodes(id: str = Query(...), translator_id: str = Query(...)):
    """Fetch seasons and episodes for a TV series under a specific voiceover"""
    mirror = config["mirror"].rstrip("/")
    ajax_url = f"{mirror}/ajax/get_cdn_series/?t={int(urllib.parse.time.time() * 1000)}"
    
    session = get_session()
    data = {
        "id": id,
        "translator_id": translator_id,
        "action": "get_episodes"
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    }

    try:
        resp = session.post(ajax_url, data=data, headers=headers, timeout=12)
        res_json = resp.json()

        if not res_json.get("success"):
            return {"success": False, "seasons": []}

        seasons_html = res_json.get("seasons", "")
        episodes_html = res_json.get("episodes", "")

        soup_s = BeautifulSoup(seasons_html, "html.parser")
        soup_e = BeautifulSoup(episodes_html, "html.parser")

        seasons_data = []
        season_items = soup_s.select(".b-simple_season__item")
        
        # If no seasons list returned, there might just be 1 season
        if not season_items:
            episodes = []
            for ep in soup_e.select(".b-simple_episode__item"):
                s_id = ep.get("data-season_id", "1")
                e_id = ep.get("data-episode_id", "1")
                episodes.append({
                    "season_id": int(s_id) if s_id.isdigit() else s_id,
                    "episode_id": int(e_id) if e_id.isdigit() else e_id,
                    "title": ep.text.strip()
                })
            seasons_data.append({
                "season_id": 1,
                "title": "1 сезон",
                "episodes": episodes
            })
        else:
            for s_el in season_items:
                s_id = s_el.get("data-tab_sub_id")
                s_title = s_el.text.strip()
                episodes = []
                for ep in soup_e.select(f".b-simple_episode__item[data-season_id='{s_id}']"):
                    e_id = ep.get("data-episode_id")
                    episodes.append({
                        "season_id": int(s_id) if s_id.isdigit() else s_id,
                        "episode_id": int(e_id) if e_id.isdigit() else e_id,
                        "title": ep.text.strip()
                    })
                seasons_data.append({
                    "season_id": int(s_id) if s_id.isdigit() else s_id,
                    "title": s_title,
                    "episodes": episodes
                })

        return {"success": True, "seasons": seasons_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stream")
def get_stream(
    id: str = Query(...),
    translator_id: Optional[str] = Query(None),
    season: Optional[str] = Query(None),
    episode: Optional[str] = Query(None)
):
    """Fetch and decode direct video stream URLs (.m3u8, .mp4) for a movie or episode"""
    mirror = config["mirror"].rstrip("/")
    ajax_url = f"{mirror}/ajax/get_cdn_series/?t={int(urllib.parse.time.time() * 1000)}"

    session = get_session()
    data = {
        "id": id,
        "translator_id": translator_id or "0",
        "action": "get_stream"
    }
    if season is not None:
        data["season"] = str(season)
    if episode is not None:
        data["episode"] = str(episode)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": mirror + "/"
    }

    try:
        resp = session.post(ajax_url, data=data, headers=headers, timeout=12)
        res_json = resp.json()

        if not res_json.get("success"):
            return {"success": False, "message": "Failed to get stream from CDN", "raw": res_json}

        raw_url = res_json.get("url", "")
        subtitle_raw = res_json.get("subtitle", "")

        decoded_stream_str = decode_trash(raw_url)
        streams = parse_playlist(decoded_stream_str)
        subtitles = parse_subtitles(subtitle_raw)

        return {
            "success": True,
            "streams": streams,
            "subtitles": subtitles
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/proxy-stream")
def proxy_stream(url: str = Query(...)):
    """Reverse proxy for video stream segments or manifests to bypass CORS/hotlink protection"""
    session = get_session()
    try:
        req = session.get(url, stream=True, timeout=15)
        response_headers = {
            "Content-Type": req.headers.get("Content-Type", "application/octet-stream"),
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*"
        }
        if "Content-Length" in req.headers:
            response_headers["Content-Length"] = req.headers["Content-Length"]

        return StreamingResponse(
            req.iter_content(chunk_size=64 * 1024),
            status_code=req.status_code,
            headers=response_headers
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", config.get("port", 3000)))
    host = os.environ.get("HOST", config.get("host", "0.0.0.0"))
    print(f"Starting HDRezka Proxy Server on http://{host}:{port}")
    uvicorn.run("server:app", host=host, port=port, reload=False)
