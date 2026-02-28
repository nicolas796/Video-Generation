# Async Video Assembly Implementation Plan

## Overview
Convert video assembly from synchronous (blocking) to asynchronous (background) processing to prevent 502 timeouts on Render.

## Architecture
- **Celery**: Background task runner
- **Redis**: Message broker (Render has managed Redis or use Redis Cloud free tier)
- **Polling**: Frontend polls for assembly status
- **Status updates**: Job status stored in DB or Redis

## Task Breakdown

### TASK 1: Add Celery + Redis Dependencies and Config
**Estimated time:** 30-45 mins
**Dependencies:** None

**Steps:**
1. Add to `requirements.txt`:
   - `celery[redis]==5.3.6`
   - `redis==5.0.1`
   - `flower==2.0.1` (optional, for monitoring)

2. Create `app/celery_app.py`:
   - Initialize Celery with Redis broker
   - Configure task serialization
   - Set task timeouts and retry policies

3. Update `config.py`:
   - Add `CELERY_BROKER_URL` (Redis URL)
   - Add `CELERY_RESULT_BACKEND` (Redis or database)

4. Update `app/__init__.py`:
   - Initialize Celery with Flask app
   - Ensure tasks auto-discover

**Testing:** Celery starts without errors locally

---

### TASK 2: Create Async Assembly Task
**Estimated time:** 45-60 mins
**Dependencies:** TASK 1

**Steps:**
1. Create `app/tasks/__init__.py` and `app/tasks/video_tasks.py`

2. Create `assemble_final_video_async` Celery task:
   - Accepts: use_case_id, script_id, options dict
   - Runs existing SmartVideoAssembler logic
   - Updates job status in Redis/DB as it progresses
   - Handles errors with retry logic
   - Returns final video path or error

3. Add task status tracking:
   - PENDING -> PROCESSING -> COMPLETED/FAILED
   - Store progress %, current step, error message
   - Use Redis keys: `assembly_job:{use_case_id}:{job_id}`

**Testing:** Task runs locally with `celery -A app.celery_app worker --loglevel=info`

---

### TASK 3: Update Assembly API for Async
**Estimated time:** 30-45 mins
**Dependencies:** TASK 2

**Steps:**
1. Update `/api/use-cases/<id>/assemble` endpoint:
   - Returns immediately with `{job_id, status: 'pending', poll_url}`
   - Triggers `assemble_final_video_async.delay(...)`

2. Add `/api/use-cases/<id>/assembly-status/<job_id>` endpoint:
   - Returns current status: PENDING/PROCESSING/COMPLETED/FAILED
   - Returns progress %, current step, result URL if done
   - Returns error message if failed

3. Update FinalVideo model if needed:
   - Add `assembly_job_id` field
   - Track job association

**Testing:** API returns job ID, status endpoint returns correct state

---

### TASK 4: Update Frontend for Async Polling
**Estimated time:** 45-60 mins
**Dependencies:** TASK 3

**Steps:**
1. Update `assembly.html` JavaScript:
   - On "Assemble" click, show "Processing..." spinner
   - Start polling `/assembly-status/<job_id>` every 3-5 seconds
   - Update progress bar/message as status changes
   - On COMPLETED: show video player with result
   - On FAILED: show error message

2. Add visual feedback:
   - Progress percentage
   - Current step ("Extracting audio...", "Merging clips...", "Adding voiceover...")
   - Estimated time remaining (optional)

3. Handle browser refresh:
   - If page reloads, check for in-progress job
   - Resume polling if job not complete

**Testing:** UI shows progress, completes successfully, handles errors gracefully

---

### TASK 5: Render Deployment Configuration
**Estimated time:** 30 mins
**Dependencies:** TASK 1-4

**Steps:**
1. Update `render.yaml`:
   - Add Redis service (or use external Redis Cloud)
   - Add Celery worker service (separate from web service)
   - Update environment variables with Redis URL

2. Create `start_worker.sh` script for Render:
   - Starts Celery worker process
   - Handles graceful shutdown

3. Add Redis environment variables to Render Dashboard:
   - `REDIS_URL` or `CELERY_BROKER_URL`

4. Test on Render staging:
   - Deploy, trigger assembly, verify async processing

**Testing:** Assembly works end-to-end on Render without timeouts

---

## Rate Limiting for Dash
**Model:** openai/gpt-5.1-codex
**Limits:** 500 RPM, 500K TPM, 900K TPD
**Target:** 80% of limits = 400 RPM, 400K TPM

**Pacing:**
- Space API calls ~150ms apart minimum
- If editing large files, batch changes to reduce calls
- Use local edits where possible (not API calls)
- Add `time.sleep(0.15)` between calls if needed

## Success Criteria
1. Assembly returns immediately with job ID (no 502)
2. Frontend shows progress updates
3. Assembly completes successfully in background
4. No worker timeouts on Render
5. Multiple assemblies can queue and process sequentially

## Notes
- Consider using Render's free Redis for dev
- For production, Redis Cloud or Upstash (free tiers)
- Celery worker needs separate service on Render (not just web service)
