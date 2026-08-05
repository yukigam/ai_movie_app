# n8n — Automated TikTok → Supabase Importer

Replace manual Telegram bot runs with webhook-triggered or scheduled n8n workflows.

## Files

| File | Purpose |
|------|---------|
| `tiktok_supabase_workflow.json` | **Ready-to-import n8n workflow** (7 nodes, standalone) |
| `scripts/tiktok_webhook.py` | (Optional) Python FastAPI webhook — use instead of RapidAPI if preferred |

## Quick Start

1. Import `tiktok_supabase_workflow.json` into n8n:
   - **n8n UI → Workflows → Add from File → select the JSON file**
2. Configure credentials and variables (see sections below)
3. Activate the workflow and send a POST request

---

## 1. Workflow Architecture

```
POST /webhook/tiktok-auto  ──►  RapidAPI Scrape  ──►  Code (parse + upload to Storage)
                                                           │
                                                     ┌─────┴─────┐
                                                     ▼           ▼
                                                (not dup)    (dup) ──► Skip
                                                     │
                                               Upsert Series
                                                     │
                                               Upsert Episode
```

### Nodes (7 total)

| # | Node | Type | Role |
|---|------|------|------|
| 1 | **Webhook** | `n8n-nodes-base.webhook` | Listens `POST /webhook/tiktok-auto` — receives `{ "url": "..." }` |
| 2 | **RapidAPI Scrape** | `n8n-nodes-base.httpRequest` | Calls RapidAPI TikTok downloader → returns metadata (title, video_url, thumbnail) |
| 3 | **Process & Upload to Storage** | `n8n-nodes-base.code` | JavaScript — parses series/episode from title, checks Supabase for duplicates, **downloads MP4 + thumbnail and uploads to Supabase Storage** via Node.js `https` |
| 4 | **Already Exists?** | `n8n-nodes-base.if` | Branches: `true` (not a dup) → insert; `false` (dup) → skip |
| 5 | **Skip (duplicate)** | `n8n-nodes-base.noOp` | Dead-end for existing episodes |
| 6 | **Upsert Series** | `n8n-nodes-base.httpRequest` | `POST /rest/v1/series` with `Prefer: resolution=merge-duplicates` |
| 7 | **Upsert Episode** | `n8n-nodes-base.httpRequest` | `POST /rest/v1/episodes` with full episode payload |

**Key point:** Node 3 (Code) does the heavy lifting — title parsing, dedup check, and binary download + Storage upload. This avoids complex binary-data chaining between HTTP Request nodes.

---

## 2. Prerequisites

### 2.1 RapidAPI Account + API Key

1. Sign up at https://rapidapi.com
2. Subscribe to a TikTok downloader API (recommended: [TikTok Downloader](https://rapidapi.com/berlin4h-studio-berlin4h/api/tiktok-downloader-download-tiktok-videos-no-watermark))
3. Copy your `x-rapidapi-key`

### 2.2 Supabase Project

Your existing Supabase project. You need:
- **Project URL** (e.g. `https://ojiaowvkdyoiebgejkxj.supabase.co`)
- **Service Role Key** (Project Settings → API → `service_role` key)

---

## 3. Configure n8n Credentials

Create two **Header Auth** credentials in n8n:

### `RapidAPI` credential

| Field | Value |
|-------|-------|
| Name | `RapidAPI` |
| Type | **Header Auth** |
| Header Name | `x-rapidapi-key` |
| Header Value | `your-rapidapi-key-here` |

### `Supabase API` credential

| Field | Value |
|-------|-------|
| Name | `Supabase API` |
| Type | **Header Auth** |
| Header Name | `apikey` |
| Header Value | `your-supabase-service-role-key` |

> The `Authorization: Bearer <key>` header is **not** needed separately — n8n's Header Auth credential sets the `apikey` header, and Supabase accepts that for REST.

---

## 4. Configure n8n Variables

The workflow uses `$vars.supabaseUrl` in the Upsert nodes. Set it in:

**Workflow → Settings → Variables → Add Variable:**

| Key | Value |
|-----|-------|
| `supabaseUrl` | `https://YOUR_PROJECT.supabase.co` |

Alternatively, edit each "Upsert Series" / "Upsert Episode" node and replace `$vars.supabaseUrl` with your hardcoded Supabase URL.

---

## 5. Configure the Code Node

Open node **"Process & Upload to Storage"** and edit the CONFIG section at the top:

```javascript
const SUPABASE_URL = 'https://YOUR_PROJECT.supabase.co';  // ← YOUR URL
const SUPABASE_KEY = 'YOUR_SERVICE_ROLE_KEY';              // ← YOUR KEY
const BUCKET       = 'videos';
```

---

## 6. Trigger the Workflow

### Via Webhook (default)

```bash
curl -X POST https://your-n8n-instance/webhook/tiktok-auto \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@shortdramatime/video/123456789"}'
```

### Via Manual Trigger

Replace the **Webhook** node with a **Manual Trigger**, then click **Execute Workflow** and provide:

```json
{ "url": "https://www.tiktok.com/@shortdramatime/video/123456789" }
```

### Via Schedule (cron)

Replace the **Webhook** node with a **Schedule Trigger** (cron: `0 */6 * * *` = every 6 hours). You'll also need a **Code** or **HTTP Request** node to generate the list of URLs to process.

---

## 7. Parsing Logic (Code Node)

The JavaScript Code node handles:

### Title extraction
Patterns matched in order:
- `Title EP.1` / `Title EP1` (case-insensitive)
- `Title — Episode 2` / `Title - Ep 3`
- `Title Eps 4`
- `Title #5`
- `Title Part 6`

### Garbage fallback
If parsed title is empty or `"Untitled"`, falls back to `@username`.

### Duplicate check
Queries `SUPABASE_URL/rest/v1/episodes?id=eq.ep-{slug}-{num}&select=id&limit=1`. Sets `already_exists: true` if found.

### Storage upload
1. Downloads MP4 from tikcdn.io via `https.get()`
2. Uploads to `POST /storage/v1/object/videos/{slug}/video_{num}.mp4`
3. Downloads thumbnail, uploads to `thumb_{num}.{ext}`
4. Falls back to original tikcdn URLs if storage upload fails

---

## 8. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No credentials found` | Create the **Supabase API** and **RapidAPI** Header Auth credentials in n8n |
| RapidAPI returns 401 | Check your API key and subscription status on RapidAPI |
| Storage upload fails | Verify the Supabase Service Role Key has `storage.objects` permissions |
| Duplicates not skipped | Ensure `episode_id` format matches existing DB records (`ep-{slug}-{num}`) |
| Webhook 404 | Check the webhook URL path; activate the workflow in n8n |

---

## 9. Alternative: Use Python Webhook (no RapidAPI)

If you prefer not to use RapidAPI, the Python webhook (`scripts/tiktok_webhook.py`) uses **ssstik.io** scraping + **yt-dlp** fallback (free, no API key).

1. Start the webhook:
   ```bash
   uvicorn scripts.tiktok_webhook:app --host 0.0.0.0 --port 8000
   ```
2. Replace **RapidAPI Scrape** node with an **HTTP Request** pointing to `POST http://localhost:8000/scrape`
3. Update the **Code** node's response-flattening logic to match the webhook's response schema.
