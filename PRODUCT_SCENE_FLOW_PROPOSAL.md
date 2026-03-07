# Product-in-Scene Video Generation Proposal

## Context
When the pipeline uses an existing product image as the primary visual input for image-to-video models like Kling, the model tends to preserve image content strongly. If text prompts ask for a different environment, scene changes are often ignored in favor of animating what is already present in the image.

This is why "single image + rich scene text" often fails for product storytelling ads.

## Recommendation (Short Answer)
Yes, the recommended flow is the right direction for product-centric clips:

1. Generate a **composite source image** that already contains product + target scene.
2. Send that composite to Kling.
3. Keep Kling prompt focused on **motion, camera behavior, and atmosphere** (not scene replacement).

This aligns with Kling's strengths (high-fidelity motion from a fixed visual anchor) and avoids fighting its bias toward preserving the input frame.

## Proposed Hybrid Model Strategy
Use a route-per-clip strategy instead of one model for all clips.

### Clip classes
- **Product Hero Clips** (product must remain accurate):
  - Composite image -> Kling image-to-video.
  - Prompt emphasizes movement, lens, pacing, lighting mood.
- **Narrative / B-roll Clips** (world-building, transitions, lifestyle moments):
  - Sora/Veo text-to-video or image+text flows.
  - Less strict product geometry requirements.
- **Bridge Clips** (product appears briefly but scene changes heavily):
  - Generate a transitional composite first, then Kling.

### Why this works
- Preserves product identity where fidelity matters.
- Uses generative world models where scene creativity matters.
- Reduces prompt conflict and increases clip-level predictability.

## Comparison With Claude's Strategy
Claude's proposal is directionally strong and highly actionable. It is largely compatible with this proposal.

### Where Claude's strategy is stronger
- More implementation-specific framing (strategy map by clip type and model defaults).
- Clearer prompt rewrite guidance (move Kling prompts to motion-only language).
- Practical effort estimate and staged API/UI work list.

### Where this proposal is stronger
- Adds policy-based routing logic and quality-gate fallback loops.
- Separates conceptual architecture from provider specifics.
- Expands UX framing with simple mode-first controls and advanced overrides.

### Differences to resolve
- **Strategy taxonomy:**
  - Claude: `composite`, `b_roll`, `product_animate`
  - This proposal: `kling_product_locked`, `world_model_broll`, `composite_then_kling`
  - Recommendation: keep Claude's shorter labels for UX, map to internal canonical enum values.
- **Model assignment policy:**
  - Claude proposes fixed defaults per clip type.
  - This proposal adds rule-based routing using constraints (`must_show_product`, `scene_change_level`).
  - Recommendation: start with Claude defaults, then evolve to rules once telemetry is collected.

### Best option (recommended)
Adopt a **merged approach**:
1. Keep Claude's concrete per-clip default map for fast implementation.
2. Keep this proposal's routing policy and quality-gate framework as phase-2 hardening.
3. Keep the composite-first flow as the default for product-critical clips.

## Productized Pipeline Design

### Step A: Add an explicit `generation_strategy` per clip
Introduce strategy values:
- `kling_product_locked`
- `world_model_broll`
- `composite_then_kling`

Each clip in the storyboard gets one strategy, selected by rules or by user override.

### Step B: Add a `composite_preprocess` stage
For strategies requiring product placement:
1. Take product cutout/asset.
2. Generate background scene (Midjourney/Pollo).
3. Composite product into scene.
4. Store resulting image as the clip's visual anchor.

### Step C: Split prompt generation into two layers
For Kling clips, generate:
- `motion_prompt`
- `camera_prompt`
- `atmosphere_prompt`

For world-model clips, generate:
- full cinematic scene prompt with narrative action.

### Step D: Add policy-driven model routing
Rules example:
- If `must_show_product=true` and `scene_change_low` -> Kling.
- If `must_show_product=false` or `scene_change_high` -> Sora/Veo.
- If both `must_show_product=true` and `scene_change_high` -> composite_then_kling.

### Step E: Quality gates
Per clip quality checks:
- Product visibility score.
- Product consistency score.
- Prompt-policy compliance.

If quality fails, retry with adjusted strategy (e.g., fallback from direct Kling to composite_then_kling).


### Step F: Support client-owned outside content
Add an ingestion layer for external assets that clients already own:
- Accepted inputs: brand footage, UGC clips, influencer shots, product photography, lifestyle stills, licensed stock references.
- Asset metadata: usage rights window, geography/channel restrictions, talent/music restrictions, and expiration date.
- Technical metadata: orientation, fps, duration, resolution, safe crop zones, and visual tags.

Routing behavior with owned assets:
- If a storyboard beat has approved owned footage, prefer **reuse** over generation.
- If owned asset partially fits, generate only bridging clips around it.
- If no suitable owned asset exists, use the strategy router (`composite_then_kling` / `world_model_broll`).

UX behavior:
- Add an "Owned Assets" library tab with drag-and-drop to storyboard cards.
- Show "rights-safe" badges and warnings before generation/assembly.
- Provide one-click actions: `Use as-is`, `Use as reference`, `Generate matching bridge clip`.

## Complexity Estimate

### Backend complexity: **Medium**
Most of the required architecture already aligns with existing clip orchestration:
- clip prompt generation,
- clip typing (hook/problem/solution/cta),
- model-specific generation services.

Main additional work:
1. New strategy field + router logic.
2. Composite image generation integration.
3. Prompt schema split by strategy.
4. Retry/fallback policy and scoring.

### UI complexity: **Low to Medium**
To keep this intuitive, avoid exposing raw model details initially.

Recommended UX:
- Add one top-level mode selector:
  - `Balanced (recommended)`
  - `Product Accuracy`
  - `Creative Storytelling`
- Optional advanced toggle: "Per-clip model routing" with editable clip cards.

This gives novices simple choices while power users can fine-tune.

### Operational complexity: **Medium**
- More providers mean credential management and cost tracking.
- Need per-model latency/cost telemetry.
- Need graceful fallback paths when provider queue is congested.

## Suggested UX Flow
1. User uploads product images and optional owned assets.
2. User selects a **Use Case** (goal, audience, platform, clip count, style constraints).
3. System generates or accepts a **Script** (editable), then maps it into clip beats (Hook/Problem/Solution/CTA, etc.).
4. User picks one of three generation modes (Balanced / Accuracy / Creative).
5. System auto-builds storyboard from use case + approved script and assigns per-clip strategies/models.
6. User sees per-clip cards:
   - Beat + linked script line(s)
   - Strategy (locked/creative/composite)
   - Model choice (auto, editable in advanced mode)
   - Asset source (generated vs owned)
7. Generate previews.
8. "Improve this clip" actions:
   - Keep product more visible
   - Make camera more dynamic
   - Make scene more cinematic
   - Stay closer to script


## Storyboard and Assembly Output
Yes — the goal is to generate **multiple clips** (storyboard-driven), then pass approved clips into the existing assembly pipeline.

Proposed contract between generation and assembly:
- Storyboard produces ordered clip specs (`sequence_order`, `clip_type`, `generation_strategy`, `model_choice`, `script_segment_ref`, `use_case_ref`).
- Generation produces per-clip outputs (video URL/path, duration, quality scores, optional alt takes).
- Assembly consumes selected takes in order, aligns them to script/voiceover timing, then applies transitions, subtitles, and final render settings.

This keeps generation and assembly decoupled:
- Generation optimizes clip quality/model routing.
- Assembly optimizes pacing, continuity, and final deliverable formatting.

## Rollout Plan

### Phase 1 (fastest value)
- Keep existing flow.
- Add strategy router + prompt schema split.
- Add manual "Use composite image" switch for Kling clips.
- Add storyboard output contract for assembly handoff (ordered clips + selected takes).
- Seed defaults with Claude's mapping:
  - Hook/Solution/CTA -> composite + Kling
  - Problem/Benefits/Social Proof -> b-roll + world model

### Phase 2
- Integrate composite generation provider.
- Add automatic composite generation for flagged clips.
- Add fallback/retry policies.
- Introduce policy-based routing rules beyond static clip-type defaults.
- Add owned-asset ingestion (metadata + rights checks + storyboard attach).

### Phase 3
- Add clip quality scoring + auto-regeneration loop.
- Add user-facing advanced per-clip routing controls.
- Add cost/latency-aware dynamic router.
- Add mixed assembly timeline (generated + owned clips with continuity assist).

## Risks and Mitigations
- **Risk:** Composite generation introduces visual artifacts.
  - **Mitigation:** Add QA check and auto-regenerate background/composite.
- **Risk:** Multi-provider cost spikes.
  - **Mitigation:** Default Balanced mode with budget-aware routing caps.
- **Risk:** User confusion with too many controls.
  - **Mitigation:** Default to simple mode, hide advanced options under "Pro settings".

## Final Recommendation
The best path is a **hybrid of Claude's implementation-first plan and this architecture-first plan**.

Specifically:
- Use composite-first Kling for product-critical clips.
- Use Sora/Veo for B-roll and narrative transitions.
- Start with clip-type defaults, then add rules and quality feedback loops.
- Keep UI simple with mode presets, and expose per-clip overrides only in advanced mode.
- Treat storyboard as the source of truth, then hand selected clips/takes to assembly.

This gets immediate quality wins quickly while preserving a scalable architecture and supporting client-owned content.

## What It Takes to Build (Execution Plan)

### Team and ownership
- **Backend (1–2 engineers):** routing engine, prompt schema updates, provider orchestration, quality gates, assembly handoff contract.
- **Frontend (1 engineer):** storyboard UX, mode selector, per-clip override controls, owned-asset library interactions.
- **Product/Design (0.5):** UX decisions, defaults, and progressive disclosure for advanced controls.
- **QA/Ops (0.5):** regression checks, provider failover validation, telemetry dashboards.

### Effort estimate (practical)
- **MVP (Phase 1): 1.5 to 2.5 weeks**
  - Per-clip strategy router.
  - Motion-only prompt path for Kling.
  - Storyboard fields needed for assembly handoff.
  - Mode presets + minimal clip override UI.
- **Phase 2: +1.5 to 2 weeks**
  - Composite generation automation.
  - Owned-asset ingestion with rights metadata.
  - Policy-based routing and fallback loops.
- **Phase 3: +1.5 to 2.5 weeks**
  - Quality scoring + regeneration loop.
  - Cost/latency-aware dynamic routing.
  - Mixed generated/owned continuity assist in assembly.

**Total:** ~4.5 to 7 weeks for a production-ready v1, depending on provider stability and design review cycles.

### Data/model changes required
- Add clip-level fields: `generation_strategy`, `model_choice`, `asset_source`, `script_segment_ref`, `quality_score`.
- Add use-case-level defaults: `default_mode`, `clip_strategy_overrides`.
- Add asset records for owned content rights and technical metadata.

### API and workflow changes
- Add/extend APIs for:
  - Storyboard generation from use case + script.
  - Per-clip strategy/model overrides.
  - Owned asset attach/detach to storyboard beats.
  - Generation run that returns clip takes + quality metrics.
  - Assembly submission endpoint that accepts selected takes.

### Testing and release gates
- **Backend tests:** routing logic matrix, fallback behavior, schema serialization, assembly contract validation.
- **Integration tests:** end-to-end use case -> script -> storyboard -> generation -> assembly.
- **UX tests:** default flow completion without advanced settings, override behavior, owned-asset rights warnings.
- **Ops gates:** provider error rate, retry success rate, generation latency, per-video cost ceilings.

### Critical dependencies and risks
- Stable provider APIs for Kling/Sora/Veo/Pollo and predictable queue times.
- Reliable rights metadata inputs for client-owned media.
- Assembly service support for mixed-source clips and script timing alignment.

### Recommended build order
1. Implement Phase 1 MVP to unlock immediate quality wins.
2. Add owned-asset ingestion + composite automation (Phase 2).
3. Add scoring/dynamic optimization and advanced continuity tooling (Phase 3).
