# Proposal: Composite-First Video Generation & Multi-Model Clip Strategy

## Problem Statement

When using image-to-video models like Kling, the model strongly prioritizes the input image as the scene anchor. If the text prompt describes a scene that conflicts with the image (e.g., "product on a kitchen counter" but the image is a white-background product shot), Kling ignores the text scene and only animates the image content. This produces clips that look like "animated product photos" rather than "product in a scene."

## Proposed Solution: Two-Pronged Approach

### 1. Composite-First Flow for Product-in-Scene Clips

Instead of: product image + scene description text → video model (conflicts)

New flow: **product + scene → composite image → video model (motion-only prompt)**

**Steps:**
1. Generate a product-in-scene composite image using DALL-E 3 (already integrated) or Pollo image gen
2. Upload that composite to Kling/video model as the input image
3. Prompt ONLY motion, camera, and atmosphere — no scene description

**Key insight:** The video model gets an image that already shows the product in the scene, so there's no text-vs-image conflict. The prompt only needs to say "slow zoom in, warm light flickers, shallow depth of field breathing" etc.

### 2. Multi-Model Strategy per Clip Role

Different clip types benefit from different models:

| Clip Role | Best Model Strategy | Rationale |
|-----------|-------------------|-----------|
| **Hook** (attention grabber) | Composite-first → Kling/Luma | Product needs to be IN a scene, animated naturally |
| **Problem** (pain point) | Text-only → Sora/Veo (B-roll) | No product needed, just relatable scenario |
| **Solution** (product hero) | Composite-first → Kling | Product solving the problem, in context |
| **Benefits** (lifestyle) | Text-only → Sora/Veo (B-roll) | Aspirational lifestyle, no specific product needed |
| **Social Proof** | Text-only → Sora/Veo (B-roll) | People enjoying results, B-roll feel |
| **CTA** (closing) | Composite-first → Kling/Luma | Product front and center, memorable |
| **Product Demo** | Product image → Kling (motion-only) | Animate the product itself |
| **Product Showcase** | Composite-first → Kling | Product in premium setting |

## Implementation Plan

### Step 1: Add `generation_strategy` field to VideoClip model

Add a new field to track which generation flow each clip uses:
- `"composite"` — Generate composite image first, then motion-only video
- `"b_roll"` — Text-only prompt, no product image (Sora/Veo)
- `"product_animate"` — Direct product image + motion-only prompt (existing flow, refined)

### Step 2: Add clip-level model override and strategy mapping

In `VideoClipManager._determine_clip_types()`, also return the recommended strategy and model for each clip type. Create a new method `_get_clip_generation_plan()` that returns:

```python
{
    'clip_type': 'hook',
    'strategy': 'composite',        # composite | b_roll | product_animate
    'recommended_model': 'kling-2.1',
    'prompt_style': 'motion_only',  # motion_only | full_scene | b_roll_scene
}
```

### Step 3: Create motion-only prompt builders

New prompt builders that generate ONLY motion/camera/atmosphere descriptions:

```python
def _build_motion_only_prompt(clip_type, style, mood):
    # Returns: "Slow cinematic zoom in, warm golden light shifts gently,
    #           shallow depth of field breathing, subtle steam rising,
    #           professional commercial quality"
    # NO scene description, NO product description
```

### Step 4: Create B-roll prompt builders

New prompt builders for text-only clips that don't include the product:

```python
def _build_b_roll_prompt(clip_type, script_segment, style):
    # Returns full scene description for Sora/Veo
    # "Person struggling with messy kitchen, frustrated expression,
    #  warm domestic lighting, realistic, 4K"
```

### Step 5: Update the generation pipeline

Modify `VideoClipManager.generate_clips_for_use_case()` to:

1. Get the clip generation plan (type + strategy + model per clip)
2. For `composite` clips:
   a. Call `_generate_scene_for_clip()` to create composite image
   b. Use composite image URL as `image_url`
   c. Use motion-only prompt
   d. Send to Kling/Luma
3. For `b_roll` clips:
   a. No image input
   b. Use full B-roll scene prompt
   c. Send to Sora/Veo (text-to-video only)
4. For `product_animate` clips:
   a. Use original product image
   b. Use motion-only prompt
   c. Send to Kling

### Step 6: Update the `UseCase` model

Add a `clip_strategies` JSON field to `UseCase` to allow users to override the default strategy per clip. This enables the user to customize which clips are composite vs B-roll.

### Step 7: Add API endpoints for strategy management

- `GET /api/use-cases/<id>/clip-plan` — Returns the recommended clip plan with strategies
- `PUT /api/use-cases/<id>/clip-plan` — User overrides strategy/model per clip
- Both return the plan showing each clip's type, strategy, model, and prompt style

### Step 8: Frontend UX (conceptual)

In the clip generation step, show a visual "storyboard" where each clip card shows:
- Clip type label (Hook, Problem, Solution...)
- Strategy badge: "Composite" (blue), "B-Roll" (green), "Product" (orange)
- Model badge: "Kling 2.1", "Sora 2", etc.
- One-click to switch strategy per clip
- Smart defaults based on clip type (user doesn't need to think about it)

## What Makes This Intuitive for Users

1. **Smart defaults** — The system automatically picks the best strategy per clip type. Users who don't care about the details just click "Generate" and get better results.

2. **Visual storyboard** — Before generation, users see a card per clip showing what will happen. Each card has a clear label and color-coded strategy.

3. **One-click overrides** — Power users can click a clip card to change its strategy (e.g., "make this a B-roll instead of composite") or model.

4. **Progressive disclosure** — Basic users see: clip type + auto strategy. Advanced users can expand to see/edit: model, prompt, strategy, composite image.

## Complexity Assessment

**Backend: Medium complexity** (~2-3 days)
- The composite image generation already exists (`_generate_scene_for_clip`)
- The multi-model routing already exists (Pollo API supports all models)
- Main work: strategy orchestration, motion-only prompts, B-roll prompts, clip plan API

**Frontend: Medium complexity** (~2 days)
- Storyboard UI with strategy badges
- Per-clip override controls
- Preview of composite images before video generation

**Risk: Low**
- All building blocks exist (DALL-E integration, multi-model Pollo API)
- Backward compatible — existing flows still work
- New strategy is additive, not a rewrite

## Files to Modify

1. `app/models.py` — Add `generation_strategy` to VideoClip, `clip_strategies` to UseCase
2. `app/services/video_clip_manager.py` — Strategy-aware clip generation, motion-only/B-roll prompts
3. `app/services/pollo_ai.py` — Add B-roll model preferences, strategy-to-model mapping
4. `app/routes.py` — New clip-plan endpoints, updated generate-clips flow
5. `app/services/clip_prompt_generator.py` — Motion-only and B-roll prompt generation
