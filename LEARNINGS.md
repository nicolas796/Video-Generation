# Video Generation Debug Learnings - 2026-02-25

## Problem Summary
Videos were failing immediately with "error" status. Root cause was a combination of issues:

## Key Findings

### 1. AI Video Generation Takes Time
- **Kling-1.6**: 2-3 minutes per video (normal)
- **Initial issue**: App expected instant results, showed errors when videos were still processing
- **Solution**: Implement polling + show "Processing (2-3 min)" message

### 2. Pollo API Response Structure
Webhook/polling response format:
```json
{
  "result": {
    "data": {
      "generations": [{
        "status": "succeed|processing|failed",
        "url": "https://...",  // Empty until complete
        "failMsg": ""          // Error message if failed
      }]
    }
  }
}
```

### 3. Status Extraction Bug
The `_extract_status_from_payload` method wasn't checking `data.generations` (only checked top-level `generations`).

**Fixed by adding:**
```python
if not generations:
    data = payload.get('data', {})
    if isinstance(data, dict):
        generations = data.get('generations')
```

### 4. Webhook Tunnel Instability
- localtunnel disconnects frequently
- **Solution**: Implement polling-based status checking instead of relying on webhooks
- Polling interval: Every 5 seconds when clips are "generating"

### 5. Model Compatibility
- **Kling-2.0**: 404 Not Found (not available)
- **Kling-1.6**: ✅ Working
- **Pollo-1.6**: Fails immediately (empty URLs)

### 6. UI Improvements Made
- Changed "Generating..." → "Processing (2-3 min)"
- Updated clip card status: "Processing<br><small>(2-3 min)</small>"
- Updated main status: "X clips processing (2-3 min each)..."

## Working Configuration
- **Model**: Kling-1.6
- **Credits**: 980 available
- **Polling**: Every 5 seconds via `/api/use-cases/{id}/clips`
- **Status flow**: pending → generating → complete|error

## Test Results
```
Task ID: cmm2pf5yk02tnbqgcvev0kx2d
Timeline:
- 0s: Status = "waiting"
- 10s: Status = "processing" 
- 2min+: Status = "succeed", URL = "https://videocdn.pollo.ai/..."
```

## Code Changes
1. `app/services/pollo_ai.py`: Fixed `_extract_status_from_payload` and `_extract_video_url`
2. `app/routes.py`: Already had `refresh_status=True` in clips endpoint
3. `templates/video_gen.html`: Updated UI messages to show time estimates

## Next Steps for Production
1. Add progress bar based on elapsed time (estimate 2-3 min per clip)
2. Implement webhook signature verification properly (Pollo secret is Base64 encoded)
3. Add retry logic for failed generations
4. Consider implementing Celery background tasks for polling
