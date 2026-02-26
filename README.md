# Product Video Generator

AI-assisted pipeline that transforms a single product URL into polished short-form videos. It combines robust scraping, structured spec sheets, configurable use cases, AI script writing, multi-model video generation, clip intelligence, ffmpeg assembly, and ElevenLabs voiceovers behind a single Flask app + web UI.

## Key Capabilities

- **Scraping & Assets** – Shopify-specific and generic scrapers with local asset download + spec-sheet previews.
- **Use Case Designer** – Configure format, tone, CTA, target audience, voice, clip counts, and duplicate/CRUD operations.
- **Script Intelligence** – GPT-4o mini powered script creation, refinement, duration tracking, and approval workflow with offline fallback.
- **Video Generation** – Prompt engine + Pollo.ai integration spanning 30+ models (Kling, Luma, Pika, Pixverse, Veo, Hailuo, etc.), webhook ingestion, thumbnailing, retries, and upload fallback.
- **Clip Analysis & Ordering** – Vision-based tagger plus narrative-aware ordering/auto-sequencing with drag & drop UI.
- **Assembly & Voiceover** – ElevenLabs voiceover caching, ffmpeg transitions, pacing alignment, and downloadable final renders.
- **Recovery & Testing** – Pipeline recovery service, resumable clip retries, and smoke tests for critical endpoints.

## Project Structure

```
product-video-generator/
├── app/
│   ├── __init__.py          # Flask factory + extension init
│   ├── models.py            # SQLAlchemy models
│   ├── routes.py            # Flask blueprint + APIs/UI routes
│   ├── scrapers/            # Shopify + generic scrapers
│   ├── services/            # AI + media services (Pollo, scripts, voiceover, ffmpeg, etc.)
│   └── utils/               # Retry + error helpers
├── templates/               # Jinja2 templates for each pipeline stage
├── static/                  # CSS/JS/assets
├── uploads/                 # Product assets, clips, final renders
├── migrations/              # Alembic migrations
├── tests/                   # Smoke tests (pytest)
├── config.py                # Environment configuration
├── requirements.txt         # Python dependencies
├── run.py                   # Flask entrypoint
└── README.md                # You are here
```

## Setup Instructions

1. **Python environment**
   ```bash
   cd /home/baill/.openclaw/workspace/product-video-generator
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

2. **Environment variables** – create `.env` (or export) with at minimum:
   ```bash
   FLASK_ENV=development
   FLASK_APP=run.py
   SECRET_KEY=dev-secret-key-change-in-production
   DATABASE_URL=sqlite:///app.db
   UPLOAD_FOLDER=./uploads
   POLLO_API_KEY=<required>
   ELEVENLABS_API_KEY=<optional for offline fallback>
   OPENAI_API_KEY=<required for scripts/vision>
   POLLO_WEBHOOK_SECRET=<optional but recommended>
   APP_BASE_URL=http://localhost:5000
   DEFAULT_VOICE_ID=XB0fDUnXU5powFXDhCwa
   ```

3. **Database**
   ```bash
   flask db upgrade   # or `python -m flask db upgrade`
   ```

4. **Run the server**
   ```bash
   flask run --host 0.0.0.0 --port 5000
   # or python run.py
   ```

5. **Front-end access** – visit `http://localhost:5000` for the pipeline dashboard plus dedicated pages:
   - `/scrape`, `/spec-sheet/<product_id>`
   - `/use-case/<product_id>`
   - `/script/<use_case_id>`
   - `/video-gen/<use_case_id>`
   - `/assembly/<use_case_id>`
   - `/output/<use_case_id>`

## API Reference

### Core & Products

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/status` | Health check |
| GET/POST | `/api/products` | List or create products |
| GET/PUT/DELETE | `/api/products/<id>` | Retrieve, update, delete product |
| POST | `/api/scrape` | Scrape URL (optional save) |
| POST | `/api/scrape/preview` | Scrape URL for preview only |

### Assets & Spec Sheets

| Method | Path | Description |
| --- | --- | --- |
| POST | `/api/products/<id>/download-images` | Mirror remote product images |
| GET | `/api/products/<id>/assets` | List local assets |
| GET | `/api/products/<id>/spec-sheet` | Structured info + assets |
| GET | `/api/products/<id>/images` | Local image picker for video prompts |
| GET | `/uploads/<path>` | Serve stored files |

### Use Cases & Voices

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/products/<id>/use-cases` | List use cases for product |
| POST | `/api/products/<id>/use-cases` | Create use case |
| GET/PUT/DELETE | `/api/use-cases/<id>` | CRUD on single use case |
| POST | `/api/use-cases/<id>/duplicate` | Duplicate configuration |
| GET | `/api/voices` | List ElevenLabs voices (fallback list if no key) |
| POST | `/api/voices/preview` | Generate preview audio snippet |

### Scripts

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/use-cases/<id>/script` | Fetch script |
| POST | `/api/use-cases/<id>/script` | Generate script |
| PUT | `/api/use-cases/<id>/script` | Update/approve script |
| POST | `/api/use-cases/<id>/script/regenerate` | Regenerate/refine |
| POST | `/api/use-cases/<id>/script/approve` | Mark as approved |
| DELETE | `/api/use-cases/<id>/script` | Remove script |

### Video Generation & Clips

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/video-models` | Available Pollo.ai models |
| GET | `/api/use-cases/<id>/clips` | List clips (+refresh status) |
| POST | `/api/use-cases/<id>/generate-clips` | Generate single clip |
| POST | `/api/use-cases/<id>/upload-video` | User-uploaded clip |
| GET | `/api/clips/<id>/status` | Poll status |
| POST | `/api/clips/<id>/regenerate` | Regenerate clip |
| DELETE | `/api/clips/<id>` | Delete clip |
| PUT | `/api/use-cases/<id>/clips/reorder` | Legacy reorder endpoint |
| PUT | `/api/clips/<id>/update-prompt` | Edit prompt before generation |
| GET | `/api/use-cases/<id>/generation-stats` | Clip progress summary |
| GET | `/api/pollo-credits` | Credit balance |
| POST | `/api/use-cases/<id>/retry-failed-clips` | Batch retries |

### Assembly, Analysis & Final Output

| Method | Path | Description |
| --- | --- | --- |
| POST | `/api/use-cases/<id>/analyze-clips` | Vision-based analysis |
| POST | `/api/use-cases/<id>/optimize-sequence` | Recommend ordering |
| POST | `/api/use-cases/<id>/apply-sequence` | Apply ordering payload |
| PUT | `/api/use-cases/<id>/clip-order` | Persist drag/drop order |
| POST | `/api/use-cases/<id>/auto-order` | Auto apply AI ordering |
| GET | `/api/use-cases/<id>/assembly` | Aggregated assembly data |
| POST | `/api/use-cases/<id>/assemble` | Run ffmpeg + voiceover pipeline |
| GET | `/api/use-cases/<id>/final-video` | Metadata for latest render |
| GET | `/api/use-cases/<id>/download` | Download rendered video |

### Pipeline Monitoring & Webhooks

| Method | Path | Description |
| --- | --- | --- |
| POST | `/webhooks/pollo` | Pollo.ai webhook receiver |
| GET | `/api/use-cases/<id>/pipeline-status` | Stage health summary |
| POST | `/api/use-cases/<id>/pipeline-recover` | Attempt automatic recovery |

## Usage Walkthrough (CLI-friendly)

```bash
# 1. Scrape and store a product
curl -X POST http://localhost:5000/api/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://store.com/products/example", "save": true}'

# 2. Create a use case configuration for product ID 1
curl -X POST http://localhost:5000/api/products/1/use-cases \
  -H 'Content-Type: application/json' \
  -d '{"name": "TikTok Hook", "format": "9:16", "style": "cinematic", "duration_target": 25, "voice_id": "XB0fDUnXU5powFXDhCwa"}'

# 3. Generate + approve a script (use_case_id=5)
curl -X POST http://localhost:5000/api/use-cases/5/script
curl -X POST http://localhost:5000/api/use-cases/5/script/approve

# 4. Trigger clip generation and poll status
curl -X POST http://localhost:5000/api/use-cases/5/generate-clips -d '{}' -H 'Content-Type: application/json'
curl http://localhost:5000/api/use-cases/5/clips

# 5. Analyze, auto-order, and assemble
curl -X POST http://localhost:5000/api/use-cases/5/analyze-clips -d '{}' -H 'Content-Type: application/json'
curl -X POST http://localhost:5000/api/use-cases/5/auto-order -d '{}' -H 'Content-Type: application/json'
curl -X POST http://localhost:5000/api/use-cases/5/assemble -d '{}' -H 'Content-Type: application/json'
```

## Testing & Tooling

```bash
source venv/bin/activate
pytest tests/test_smoke.py -v
```

Smoke tests cover the Flask blueprint wiring, CRUD flows, and pipeline recovery endpoints.

## Operational Notes

- **Uploads:** All generated assets live under `uploads/` (products, clips, final). Ensure the folder is writable before running the app.
- **Webhooks:** For Pollo.ai callbacks, expose `/webhooks/pollo` via a tunnel (ngrok/cloudflared) and set `POLLO_WEBHOOK_SECRET` + `APP_BASE_URL`.
- **Rate Limiting:** Script + vision services respect retry/backoff via `app/utils/retry.py`. When operating under strict OpenAI quotas, stagger requests (12–15s spacing) or rely on offline fallbacks built into the services.
- **Recovery:** Use `/api/use-cases/<id>/pipeline-recover` to automatically regenerate scripts, restart clips, rerun analysis, and rebuild final renders. This is safe to call repeatedly.
- **ffmpeg:** Ensure `ffmpeg`/`ffprobe` are installed and on `$PATH`. Errors bubble up with user-friendly messages in the UI/API.

---
Questions or issues? Start by checking `flask.log`, the `uploads/` directory for missing assets, and the smoke tests for regression clues.
