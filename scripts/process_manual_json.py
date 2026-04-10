import sys
import json
from pathlib import Path
from datetime import datetime

# Local imports fix
sys.path.append(str(Path(__file__).parent))

from main import (
    load_dotenv, ENV_PATH, _load_cache, _save_cache, filter_activity, batch_llm_analyze, 
    _is_cache_fresh, format_date, parse_subs, generate_report, PROJECT_ROOT, SKILL_ROOT,
    BATCH_TOPIC, logger, extract_contacts_from_links, extract_contacts_regex
)

def run_pure_manual(file_name=None):
    """
    Runs the pipeline WITHOUT Apify API calls.
    Uses a pre-scraped JSON dataset from the 'scratch' directory.
    """
    if file_name:
        manual_json_path = PROJECT_ROOT / "scratch" / file_name
    else:
        # Default fallback to a generic name
        manual_json_path = PROJECT_ROOT / "scratch" / "youtube_dataset.json"
        
    logger.info(f"🚀 Running manual data processing from: {manual_json_path.name}")
    
    if not manual_json_path.exists():
        logger.error(f"Dataset not found: {manual_json_path}")
        return

    with open(manual_json_path, encoding="utf-8") as f:
        manual_data = json.load(f)
    
    # 1. Aggregate channel data from individual video items
    channels_map = {}
    for item in manual_data:
        info = item.get("aboutChannelInfo", {})
        c_url = info.get("channelUrl") or item.get("channelUrl")
        if not c_url: continue
        
        if c_url not in channels_map:
            bio = info.get("channelDescription", "")
            links = info.get("channelDescriptionLinks", [])
            
            # Extract contacts using unified helpers
            contacts = extract_contacts_from_links(links)
            if not any(contacts.values()):
                contacts = extract_contacts_regex(bio)
                
            channels_map[c_url] = {
                "url": c_url,
                "name": info.get("channelName", "Unknown"),
                "subs": parse_subs(info.get("numberOfSubscribers") or 0),
                "bio": bio,
                "contacts": contacts,
                "videos": [],
                "is_active": False,
                "is_relevant": False,
                "relevance_reason": ""
            }
        
        # Collect video metadata
        if v_date := item.get("date"):
            channels_map[c_url]["videos"].append({
                "title": item.get("title", ""),
                "date": v_date,
                "views": item.get("viewCount", "-"),
                "url": item.get("url", "")
            })

    # 2. Post-processing: Sort videos to isolate the absolute latest
    channels_list = []
    for c_url, c in channels_map.items():
        if not c["videos"]: continue
        
        c["videos"].sort(key=lambda x: x["date"], reverse=True)
        latest = c["videos"][0]
        
        c.update({
            "video_title": latest["title"],
            "video_views": latest["views"],
            "video_url": latest["url"],
            "video_date": format_date(latest["date"]),
            "latest_videos": [v["title"] for v in c["videos"][:3]]
        })
        channels_list.append(c)

    logger.info(f"✅ Aggregated {len(channels_list)} unique channels from JSON dataset.")
    
    # 3. Cache Check: Restore previously gathered AI analysis results
    cache = _load_cache()
    for c in channels_list:
        if entry := cache.get(c["url"]):
            if _is_cache_fresh(entry):
                c["is_relevant"] = entry.get("is_relevant", False)
                c["relevance_reason"] = entry.get("relevance_reason", "")
                if entry.get("contacts") and any(entry["contacts"].values()):
                    c["contacts"] = entry["contacts"]

    # 4. Filter by activity (default 90 days threshold)
    filtered = filter_activity(channels_list)
    
    # 5. LLM Analysis (Gemini) - Execute exclusively for uncached active channels
    batch_llm_analyze(filtered, cache)
    
    # 6. Save modified cache gracefully
    _save_cache(cache)
    
    # 7. Generate markdown Report
    now = datetime.now()
    report_filename = PROJECT_ROOT / "db" / "reports" / f"manual_{BATCH_TOPIC}_{now.strftime('%d_%m_%H%M')}.md"
    generate_report(filtered, report_filename)
    
    logger.info(f"🎯 Report created successfully: {report_filename}")

if __name__ == "__main__":
    fname = sys.argv[1] if len(sys.argv) > 1 else None
    run_pure_manual(fname)
