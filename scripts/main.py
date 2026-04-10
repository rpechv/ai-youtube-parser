import os
import re
import json
import math
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from dateutil.parser import parse as parse_date
from apify_client import ApifyClient
from dotenv import load_dotenv

# ================= ROOT PATH =================
SKILL_ROOT = Path(__file__).parent.parent.resolve()
# Global project root is one level above the skill folder
PROJECT_ROOT = SKILL_ROOT.parent.resolve()

# ================= LOGGING =================
log_dir = PROJECT_ROOT / "db" / "reports"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "parser_debug.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Check for google-genai availability
try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logger.error("google-genai is not installed. Run: pip3 install google-genai")

# ================= Configuration =================
BATCH_TOPIC             = "cooking_recipes_test"
SEARCH_QUERIES = [
    "как готовить медовик",
    "как готовить фаршированые перцы"
]

MAX_RESULTS_PER_QUERY   = 5
MIN_SUBS                = 3_000
MAX_SUBS                = 70_000
MAX_SUBS_HARD_LIMIT     = 600_000   # Auto-exclude channels larger than this to save LLM tokens
ACTIVITY_DAYS           = 90        # Activity threshold for Lists 1 & 2
HARD_DELETE_DAYS        = 210       # Fully remove from report if video is > 7 months old
LLM_BATCH_SIZE          = 50        # Channels per Gemini request
APIFY_TOKEN_START_IDX   = 0         
CACHE_DAYS              = 90        # LLM analysis cache lifetime

GEMINI_MODEL_NAME       = "gemini-3.1-flash-lite-preview"

TOPIC_PROMPT = (
    "Channels about cooking, food recipes, specifically desserts (Medovik) and traditional main courses (stuffed peppers). "
    "Focus on high-quality home cooking and professional culinary instructions."
)

# ================= ENV =================
# Try to load env from skill folder first, then global
ENV_PATH = SKILL_ROOT / ".env"
load_dotenv(ENV_PATH)

APIFY_TOKENS = [t.strip() for t in os.getenv("APIFY_TOKEN", "").split(",") if t.strip()]
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not APIFY_TOKENS:
    logger.error("❌ APIFY_TOKEN is missing in .env")
    exit(1)
if not GOOGLE_API_KEY:
    logger.error("❌ GOOGLE_API_KEY is missing in .env")
    exit(1)
if not GEMINI_AVAILABLE:
    exit(1)

logger.info(f"✅ Apify tokens: {len(APIFY_TOKENS)}, starting with #{APIFY_TOKEN_START_IDX + 1}")
_token_idx = APIFY_TOKEN_START_IDX

genai_client = genai.Client(api_key=GOOGLE_API_KEY)
logger.info(f"✅ LLM: {GEMINI_MODEL_NAME} (google.genai)")

# ================= CACHE =================
CACHE_PATH = PROJECT_ROOT / "db" / "youtube_channels.json"

def _load_cache() -> dict:
    """Loads channel cache from JSON."""
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_cache(cache: dict):
    """Saves cache to disk."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, default=str)

def _is_cache_fresh(entry: dict) -> bool:
    """Checks if entry is still within CACHE_DAYS."""
    ts = entry.get("cached_at")
    if not ts:
        return False
    try:
        cached_at = datetime.fromisoformat(ts)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - cached_at).days < CACHE_DAYS
    except Exception:
        return False

# ================= Apify helpers =================
def get_apify_client():
    return ApifyClient(APIFY_TOKENS[_token_idx])

def rotate_token(reason=""):
    """Rotates to the next Apify token if the current one hits a limit/quota."""
    global _token_idx
    if _token_idx + 1 < len(APIFY_TOKENS):
        _token_idx += 1
        logger.warning(f"🔄 Rotating to token #{_token_idx + 1} ({reason})")
        return True
    logger.error(f"❌ All Apify tokens exhausted. {reason}")
    return False

def apify_call(actor_id, run_input, min_expected=0, fields=None):
    """Apify Actor call with automatic token rotation using structural pattern matching for HTTP errors."""
    global _token_idx
    while True:
        try:
            client = get_apify_client()
            logger.info(f"   ⚡ Using Token #{_token_idx + 1}: {APIFY_TOKENS[_token_idx][:15]}...")
            run = client.actor(actor_id).call(run_input=run_input)
            items = list(client.dataset(run["defaultDatasetId"]).iterate_items(fields=fields))
            if len(items) <= min_expected:
                logger.warning(f"⚠️ Token #{_token_idx + 1} returned only {len(items)} items.")
                if rotate_token("low item count"):
                    continue
            return items
        except Exception as e:
            err_msg = str(e).lower()
            # Handle HTTP errors based on keywords in the exception message
            if any(k in err_msg for k in ["402", "limit", "quota", "payment"]):
                if not rotate_token("usage limit"):
                    return []
            elif "403" in err_msg:
                if not rotate_token("forbidden"):
                    return []
            else:
                logger.error(f"❌ Apify error: {e}")
                raise

# ================= Utils =================
def format_date(s):
    """Parses a date string and ensures UTC timezone."""
    try:
        if not s: 
            return None
        dt = parse_date(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception: 
        return None

def parse_subs(val):
    """Parses subscriber strings (e.g., '1.5M', '300K') into integer."""
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        s = val.upper().strip()
        try:
            if "K" in s: return int(float(s.replace("K", "")) * 1_000)
            if "M" in s: return int(float(s.replace("M", "")) * 1_000_000)
            return int(float(re.sub(r"[^\d.]", "", s)))
        except Exception: 
            return 0
    return 0

def clean_txt(text):
    """Removes problematic characters from text formatting for markdown."""
    return text.replace("|", "/").replace("[", "(").replace("]", ")").strip() if text else ""

def extract_contacts_regex(text):
    """Extracts contacts via regex mappings from a text block."""
    if not text: 
        return {"email": "", "telegram": "", "instagram": ""}
    
    # Modern direct mapping
    return {
        "email": next(iter(re.findall(r"[\w\.-]+@[\w\.-]+\.\w+", text)), ""),
        "telegram": (lambda tg: f"@{tg[0]}" if tg else "")(re.findall(r"t\.me/(?:@)?([\w\d\_]+)", text)),
        "instagram": (lambda ig: f"@{ig[0]}" if ig else "")(re.findall(r"instagram\.com/([\w\d\._]+)", text))
    }

def extract_contacts_from_links(links):
    """Extracts contacts directly from a list of link objects."""
    contacts = {"email": "", "telegram": "", "instagram": ""}
    if not links or not isinstance(links, list): 
        return contacts
        
    for link in links:
        url = (link.get("url") or "").lower()
        if "t.me/" in url:
            if m := re.search(r"t\.me/(?:@)?([\w\d\_]+)", url):
                contacts["telegram"] = f"@{m.group(1)}"
        elif "instagram.com/" in url:
            if m := re.search(r"instagram\.com/([\w\d\._]+)", url):
                contacts["instagram"] = f"@{m.group(1)}"
        elif "mailto:" in url:
            contacts["email"] = url.replace("mailto:", "").split("?")[0]
                
    return contacts

# ================= 1. SEARCH =================
def fetch_youtube_search(queries):
    """Fetches YouTube channels based on keyword queries."""
    logger.info(f"--- [1/5] SEARCH: {len(queries)} queries ---")
    fields = ["channelUrl", "authorUrl", "channelName", "author", "numberOfSubscribers", "channelSubscribers", "title", "viewCount", "date", "publishedAt", "url"]
    SEARCH_BATCH_SIZE = 50
    
    all_items = []
    for i in range(0, len(queries), SEARCH_BATCH_SIZE):
        batch = queries[i : i + SEARCH_BATCH_SIZE]
        logger.info(f"   Search batch {i//SEARCH_BATCH_SIZE + 1}...")
        items = apify_call("streamers/youtube-scraper", {"searchQueries": batch, "maxResults": MAX_RESULTS_PER_QUERY, "maxResultsShorts": 0}, min_expected=2, fields=fields)
        all_items.extend(items)
        
    logger.info(f"✅ Found {len(all_items)} potential leads.")
    return all_items

# ================= 2. DEDUP & PRE-FILTER =================
def process_channels(items):
    """Deduplicates channels and filters out massive accounts."""
    logger.info(f"--- [2/5] DEDUP & PRE-FILTER (>{MAX_SUBS_HARD_LIMIT//1000}k) ---")
    channels = {}
    skipped_large = 0
    
    for item in items:
        url = item.get("channelUrl") or item.get("authorUrl")
        if not url: continue
        
        subs = parse_subs(item.get("numberOfSubscribers") or item.get("channelSubscribers") or 0)
        if subs > MAX_SUBS_HARD_LIMIT:
            skipped_large += 1
            continue
            
        v_date = format_date(item.get("date") or item.get("publishedAt"))
        
        if url not in channels:
            channels[url] = {
                "name": item.get("channelName") or item.get("author") or "Unknown", 
                "url": url, 
                "subs": subs, 
                "bio": "", 
                "video_title": item.get("title", ""), 
                "video_views": item.get("viewCount", 0), 
                "video_url": item.get("url", ""), 
                "video_date": v_date, 
                "latest_videos": [], 
                "is_active": False, 
                "is_relevant": False, 
                "relevance_reason": "", 
                "contacts": {}
            }
        else:
            ch = channels[url]
            if v_date and (not ch["video_date"] or v_date > ch["video_date"]):
                ch.update({
                    "video_date": v_date,
                    "video_title": item.get("title", ""),
                    "video_url": item.get("url", "")
                })
                
    logger.info(f"✅ {len(channels)} unique channels (skipped giants: {skipped_large}).")
    return list(channels.values())

# ================= 3. ENRICH =================
def enrich_all_channels(channels_list, cache: dict):
    """Fetches deep metadata for channels that are not cached."""
    logger.info(f"--- [3/5] ENRICH: {len(channels_list)} channels ---")
    
    to_enrich = []
    for c in channels_list:
        if entry := cache.get(c["url"]):
            if _is_cache_fresh(entry):
                c.update({
                    "bio": entry.get("bio", ""),
                    "contacts": entry.get("contacts", {}),
                    "is_relevant": entry.get("is_relevant", False),
                    "relevance_reason": entry.get("relevance_reason", ""),
                    "subs": entry.get("subs", c["subs"])
                })
                if cached_dt := format_date(entry.get("video_date")):
                    if not c["video_date"] or cached_dt > c["video_date"]:
                        c["video_date"] = cached_dt
                continue
        to_enrich.append(c)
    
    if not to_enrich:
        logger.info("✅ All channels in cache.")
        return channels_list

    fields = ["aboutChannelInfo", "date", "title", "url", "viewCount"]
    ENRICH_BATCH_SIZE = 25
    results_by_channel = {}
    
    for i in range(0, len(to_enrich), ENRICH_BATCH_SIZE):
        batch = to_enrich[i : i + ENRICH_BATCH_SIZE]
        logger.info(f"   Enrichment batch {i//ENRICH_BATCH_SIZE + 1}: {len(batch)} channels...")
        items = apify_call("grow_media/youtube-channel-scraper", {"channelUrls": [c["url"] for c in batch], "maxVideos": 3}, fields=fields)
        
        for item in items:
            info = item.get("aboutChannelInfo", {})
            key = info.get("inputChannelUrl") or item.get("channelUrl")
            if not key: continue
            
            results_by_channel.setdefault(key, {
                "bio": info.get("channelDescription", ""), 
                "subs": parse_subs(info.get("numberOfSubscribers") or 0), 
                "links": info.get("channelDescriptionLinks", []), 
                "videos": []
            })
            
            if item.get("date"):
                results_by_channel[key]["videos"].append({
                    "title": item.get("title", ""), 
                    "date": item.get("date"), 
                    "url": item.get("url", ""), 
                    "views": item.get("viewCount", 0)
                })

    for c in to_enrich:
        if res := results_by_channel.get(c["url"]):
            c["bio"] = res["bio"]
            c["subs"] = res["subs"] if res["subs"] > 0 else c["subs"]
            c["contacts"] = extract_contacts_from_links(res["links"])
            
            if not any(c["contacts"].values()): 
                c["contacts"] = extract_contacts_regex(c["bio"])
                
            if videos := sorted(res["videos"], key=lambda v: v["date"] or "", reverse=True):
                c["latest_videos"] = [v["title"] for v in videos[:3] if v["title"]]
                if real_date := format_date(videos[0]["date"]):
                    c.update({
                        "video_date": real_date,
                        "video_title": videos[0]["title"],
                        "video_url": videos[0]["url"]
                    })
                    
    return channels_list

# ================= 4. ACTIVITY =================
def filter_activity(channels_list):
    """Filters channels based on the recency of their videos."""
    logger.info(f"--- [4/5] ACTIVITY FILTER ({ACTIVITY_DAYS}d / {HARD_DELETE_DAYS}d) ---")
    today = datetime.now(timezone.utc)
    
    filtered, active, inactive, deleted = [], 0, 0, 0
    for c in channels_list:
        if not c.get("video_date"): 
            deleted += 1
            continue
            
        days_diff = (today - c["video_date"]).days
        if days_diff > HARD_DELETE_DAYS: 
            deleted += 1
            continue
            
        c["is_active"] = days_diff <= ACTIVITY_DAYS
        if c["is_active"]: active += 1
        else: inactive += 1
        filtered.append(c)
        
    logger.info(f"✅ Active: {active} | Inactive: {inactive} | Excluded (Deleted): {deleted}")
    return filtered

# ================= 5. LLM =================
def batch_llm_analyze(channels_list, cache: dict):
    """Analyzes uncached active channels using Google Gemini AI structured outputs."""
    active_uncached = [c for c in channels_list if c["is_active"] and not _is_cache_fresh(cache.get(c["url"], {}))]
    
    if not active_uncached:
        logger.info("⚠️ No new active channels for LLM.")
        return channels_list

    n = math.ceil(len(active_uncached) / LLM_BATCH_SIZE)
    logger.info(f"--- [5/5] LLM BATCH: {len(active_uncached)} channels in {n} batch(es) ---")
    url_map = {c["url"]: c for c in channels_list}
    
    for i in range(n):
        batch = active_uncached[i * LLM_BATCH_SIZE : (i + 1) * LLM_BATCH_SIZE]
        logger.info(f"   Batch {i+1}/{n}: Processing {len(batch)} channels...")
        
        payload = [{"url": c["url"], "name": c["name"], "bio": (c["bio"] or "")[:600], "latest_videos": c["latest_videos"]} for c in batch]
        prompt = f"Analyze if these channels are relevant to: {TOPIC_PROMPT}\nReturn JSON array: [{{ \"url\": \"...\", \"is_relevant\": true/false, \"reason\": \"...\", \"contacts\": {{ \"email\": \"\", \"telegram\": \"\", \"instagram\": \"\" }} }}]\nChannels:\n{json.dumps(payload, ensure_ascii=False)}"

        max_retries = 3
        for retry in range(max_retries):
            try:
                resp = genai_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
                reply = resp.text.strip()
                
                if "```json" in reply: reply = reply.split("```json")[-1].split("```")[0]
                elif "```" in reply: reply = reply.replace("```", "")
                
                for r in json.loads(reply.strip()):
                    if c := url_map.get(r.get("url")):
                        c["is_relevant"] = r.get("is_relevant", False)
                        c["relevance_reason"] = r.get("reason", "")
                        c["contacts"] = r.get("contacts", {})
                        cache[c["url"]] = {
                            "bio": c["bio"], 
                            "subs": c["subs"], 
                            "video_date": c["video_date"].isoformat() if c["video_date"] else None, 
                            "is_relevant": c["is_relevant"], 
                            "relevance_reason": c["relevance_reason"], 
                            "contacts": c["contacts"], 
                            "cached_at": datetime.now(timezone.utc).isoformat()
                        }
                logger.info("   ✅ Success.")
                break
            except Exception as e:
                err_msg = str(e)
                if "503" in err_msg and retry < max_retries - 1:
                    wait = (retry + 1) * 15
                    logger.warning(f"   ⚠️ Gemini 503. Retry {retry + 1} in {wait}s...")
                    time.sleep(wait)
                else: 
                    logger.error(f"   ❌ LLM Error: {e}")
                    break
                    
        if i < n - 1: 
            time.sleep(4)

    # Fallback to regex if LLM missed contacts but they exist in bio
    for c in channels_list:
        if not c.get("contacts") or not any(c["contacts"].values()): 
            c["contacts"] = extract_contacts_regex(c["bio"])
            
    return channels_list

# ================= 6. REPORT =================
def generate_report(channels_list, filename):
    """Generates a markdown report mapping channels to targets and inactivity."""
    logger.info(f"--- [6/6] REPORT: {filename} ---")
    
    list1, list2, inactive = [], [], []
    
    for c in channels_list:
        c["dt"] = c["video_date"].strftime("%Y-%m-%d") if c.get("video_date") else "-"
        
        con = c.get("contacts", {})
        contact_parts = [
            f"Email: {con['email']}" if con.get("email") else "",
            f"TG: {con['telegram']}" if con.get("telegram") else "",
            f"IG: {con['instagram']}" if con.get("instagram") else ""
        ]
        c["contacts_str"] = ", ".join(filter(None, contact_parts)) or "-"
        
        if c.get("is_active"):
            if c.get("is_relevant"):
                if MIN_SUBS <= c["subs"] <= MAX_SUBS: 
                    list1.append(c)
                else: 
                    list2.append(c)
        else: 
            inactive.append(c)

    # Sort lists to prioritize bigger channels
    list1.sort(key=lambda x: x["subs"], reverse=True)
    list2.sort(key=lambda x: x["subs"], reverse=True)
    inactive.sort(key=lambda x: x["subs"], reverse=True)

    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# YouTube Influencer Discovery: {BATCH_TOPIC}\n")
        f.write(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n")
        
        def _write_table(file_obj, title, rows):
            file_obj.write(f"## {title}\n")
            file_obj.write("| Channel | Subs | Contacts | Latest Video | Date | Reason |\n")
            file_obj.write("|---|---:|---|---|---|---|\n")
            for ch in rows:
                v_title = clean_txt(ch.get("video_title", "Video")[:60])
                v_link = f"[{v_title}]({ch.get('video_url', '#')})"
                file_obj.write(f"| [{clean_txt(ch['name'])}]({ch['url']}) | {ch['subs']:,} | {clean_txt(ch['contacts_str'])} | {v_link} | {ch['dt']} | {clean_txt(ch['relevance_reason'])} |\n")
            if not rows: file_obj.write("No channels found.\n")
            file_obj.write("\n")

        _write_table(f, f"List 1: Target ({MIN_SUBS//1000}k-{MAX_SUBS//1000}k)", list1)
        _write_table(f, "List 2: Other Relevant", list2)
        
        f.write("## List 3: Inactive\n")
        f.write("| Channel | Subs | Last Video | Contacts |\n|---|---:|---|---|\n")
        for c in inactive:
            f.write(f"| [{clean_txt(c['name'])}]({c['url']}) | {c['subs']:,} | {c['dt']} | {clean_txt(c['contacts_str'])} |\n")
        if not inactive: f.write("No inactive channels.\n")

    logger.info(f"✅ Report created: L1({len(list1)}) L2({len(list2)}) L3({len(inactive)})")

def main():
    """Main execution entry point."""
    logger.info("🚀 Starting Full Pipeline...")
    cache = _load_cache()
    
    items = fetch_youtube_search(SEARCH_QUERIES)
    channels = process_channels(items)
    channels = enrich_all_channels(channels, cache)
    channels = filter_activity(channels)
    channels = batch_llm_analyze(channels, cache)
    
    _save_cache(cache)
    now = datetime.now()
    report_name = PROJECT_ROOT / "db" / "reports" / f"{BATCH_TOPIC}_{now.strftime('%d_%m_%H%M')}.md"
    generate_report(channels, report_name)
    logger.info(f"🎯 Done: {report_name}")

if __name__ == "__main__": 
    main()
