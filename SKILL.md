---
name: ai-youtube-scraper
description: Run automated YouTube influencer research pipeline. Make sure to use this skill whenever the user mentions "find YouTube influencers", "scrape YouTube channels", "analyze YouTube leads", or wants to extract contacts, filter inactive channels, or verify channel relevance using AI (Gemini). Use this skill even if the user doesn't explicitly ask for a "scraper" or "script", but wants to find influencers!
---

# YouTube Scraper Skill

This skill provides an automated multi-stage Python pipeline (bundled in the `scripts/` folder) for discovering, enriching, and qualifying YouTube influencers using Apify and Google Gemini AI.

## Project Structure
- `scripts/main.py`: Full automation (Search -> Enrich -> AI Analyze -> Report).
- `scripts/process_manual_json.py`: Manual processing of pre-scraped Apify JSON files.
*(Note: Working directories like `db/` for databases/reports and `scratch/` for JSON datasets will automatically build in the User's Root workspace, not inside the skill folder.)*

## How It Works
1. **Search**: Keyword search via Apify.
2. **Pre-Filter**: Excludes massive channels (>600k subs).
3. **Enrichment**: Fetches bio, social links, and activity data via Apify.
4. **Activity Filter**: Checks the last uploaded video date (discards inactive > 90d).
5. **AI Analysis**: Uses Gemini 3.1 to verify exact niche relevance and extract Telegram/Email from the raw text.
6. **Report**: Generates an exhaustive MD table prioritizing channels with valid contacts.

## Instructions for Agents

### 1. Planning a Search
When the user asks to find influencers, first propose a list of YouTube search queries.
To change the queries, open `main.py` and modify the `SEARCH_QUERIES` list.

### 2. Configuration Settings (Crucial before execution)
Adjust the following constants in `scripts/main.py` based on the user's criteria:
- `MAX_RESULTS_PER_QUERY`: Scraping depth for each request.
- `SEARCH_QUERIES`: Targeted YouTube search phrases.
- `MIN_SUBS` / `MAX_SUBS` / `MAX_SUBS_HARD_LIMIT`: Subscriber target segment and maximum size to filter out.
- `ACTIVITY_DAYS` / `HARD_DELETE_DAYS`: Expiration boundaries for when to label channels inactive or delete them entirely.
- `TOPIC_PROMPT`: The strict prompt string instructing the AI what exactly constitutes a "relevant" channel.

### 3. Execution
To run the full pipeline automatically, execute:
```bash
python3 scripts/main.py
```

If the user specifically asks you to process a local dataset without spending Apify credits, execute:
```bash
python3 scripts/process_manual_json.py
```

### 4. Post-run Analysis
After the script finishes, locate the latest report in `db/reports/` using the `list_dir` tool (at the root of the user's workspace) and read it.
Summarize the results for the user, highlighting the highest quality leads found in "List 1".

## Dependencies
Ensure `.env` contains valid `GOOGLE_API_KEY` and `APIFY_TOKEN` (multiple tokens can be separated by commas).
If the environment throws import errors, ensure dependencies are installed via `pip install -r requirements.txt`.
