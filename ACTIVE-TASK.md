# Hook-First Video Generation — ACTIVE

**Date:** March 6, 2026  
**Owner:** Subagent (hook-feature-dash)

## Goal
Insert a dedicated "Hook" stage between Use Case and Script so creators can choose from nine proven hook formulas, preview three static variants (image + audio), pick a winner, animate a 5-second video just for that hook, and then expand the winning hook into the full script.

## Deliverables
- Database support for hooks (models + migrations + UseCase linkage)
- Hook generation service (9 formulas, GPT-4 path, deterministic fallback)
- Hook image generation service (FLUX 2 Pro integration + local asset storage)
- REST endpoints + UI routes for hook selection, preview, selection, animation, and status polling
- Celery task to animate the selected hook via existing video infra (Pollo/minimax/etc.)
- Hook selection template + JS interactivity
- Script generator integration to expand the selected hook into the full script
- Pipeline progress update so dashboards recognize the new stage

## Phase Plan & Status
1. **Database & Models** — _Complete_
   - [x] Add `Hook` model mirroring implementation plan
   - [x] Link `UseCase` ⇄ `Hook` (FK + relationship + helper fields)
   - [x] Generate + run migration (with safety checks + SKIP_DB_CREATE_ALL flag)
2. **Hook Generator Service** — _Complete_
   - [x] Added `app/services/hook_generator.py` with all 9 formulas referencing the Video Hook Generator skill
   - [x] AI path now uses `openai/gpt-5.1-codex` with a baked-in 120ms delay between calls and deterministic template fallback fed by product data
   - [x] Wire service into routes/endpoints once hook stage API is implemented
3. **Hook Image Generator Service** — _Complete_
   - [x] Added `app/services/hook_image_generator.py` with FLUX 2 Pro integration, prompt builder, and local asset storage in `/uploads/hooks/{hook_id}`
   - [x] Implemented rate-limited API calls (120ms pacing), product-aware visual angles, and robust download/base64 handling that returns relative file paths
4. **Hook API Routes & Views** — _In Progress_
   - [x] Added `/hook/<use_case_id>` UI route plus new API endpoints for creation, previews, selection, animation, and status polling
   - [x] Implemented `templates/hook.html` with hook selector, preview grid, audio players, and animation controls
   - [x] Added ElevenLabs-powered audio previews and FLUX image generation into the hook pipeline
   - [x] Tie animation into background Celery workflow (async hook animation task + status polling)
5. **Celery Hook Video Task** — _Complete_
   - [x] Added `generate_hook_video` Celery task with ffmpeg Ken Burns effect, audio sync, and status updates
   - [x] `/api/hooks/<id>/animate` now queues the task and returns Celery job metadata
6. **Frontend Hook UI** — _In Progress_
   - [x] Added dedicated Hook Builder page with Bootstrap styling and JS state management
   - [x] Async UX polish: status polling, responsive cards, enhanced loading/error states, variant selection & auto-redirect into Script
   - [ ] Final QA pass + user testing guardrails
7. **Script Integration** — _Pending_
8. **Pipeline Progress Update** — _Complete_
   - [x] Inserted Hook stage into dashboard defaults + product stage calculations
   - [x] Updated `PipelineProgressTracker` to track hook readiness alongside scripts

## Resources / References
- `/home/baill/.openclaw/workspace/HOOK_IMPLEMENTATION_PLAN.md`
- Existing services in `app/services` for structure/style
- Render deployment constraints (keep dependencies + env vars compatible)

## Reporting
- Update this checklist at each phase
- Summaries + blockers will be reported back to main agent when milestones shift or blockers arise
