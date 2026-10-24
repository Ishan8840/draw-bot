# Camera-only chalk feedback


## Live drawing with Astra

`python -m scripts.run_mujoco` enables camera verification by default (`SIM_VERIFY_MARKS=true`). All strokes use the same universal executor. Each stroke is checked immediately after its normal pen lift, **before the next stroke starts**. If the image cannot confirm the stroke, the arm moves aside and checks again. Occlusion alone never authorizes lowering the chalk. There is no wait for the entire drawing to finish.

A confirmed missing stroke is retried immediately with a fresh ID, inserted ahead of the remaining strokes in the same semantic object. Clearly localized gaps can also be repaired without retracing the good part. At most three repair strokes per original stroke and a persistent total height correction of 0.75 mm are allowed, in 0.25 mm increments. Cancel/Clear/restart do not replenish this budget. Faint, ambiguous or exhausted results pause drawing and revoke stale model commands.

Astra receives `mark_verification` and `drawing_feedback` through the scene and camera context. Camera events appear in chat; a failure triggers a read-only Astra explanation. Only a new user instruction can resume or modify the paused plan. Stop, Cancel and Clear remain responsive while images are processed, and stale checks cannot apply corrections.

The previous usable post-stroke image is reused as the next baseline; initial or invalidated baselines require a clear capture. Successful checks do not unconditionally command a park, but the current overhead camera is often occluded by the arm, so moving aside may still be needed between strokes. A restart before verification completes pauses conservatively because baseline images are not persisted. The detector remains a controlled-scene approximation, not proof of contact pressure or hardware readiness.

To reproduce wear in the actual app, export settings before starting the server:

```sh
SIM_CHALK_LOSS_M=0.0006 SIM_VERIFY_MARKS=true python -m scripts.run_mujoco --database data/chalk-live-test.sqlite3
```

Ask Astra to draw a short line. The environment shortens the physical nib, while the controller and Astra remain unaware of that hidden amount. Try `SIM_CHALK_LOSS_M=0.003` to exhaust the budget, or `SIM_CHALK_WEAR_RATE=0.02` for progressive wear. Use a fresh test database when you deliberately want a fresh correction budget. The normal `scripts.run_mujoco` launcher now defaults to a wear rate of `0.002` (2 mm of tip shortening per meter of deposited line) and initial loss remains zero. Explicit `SIM_CHALK_WEAR_RATE=0` disables wear. Direct Uvicorn launches retain the backend fallback of zero unless configured. The rate is an illustrative simulation setting, not measured chalk material data. Environment wear is reinitialized from these settings on restart, whereas the correction budget persists; these fault-injection scenarios are not a calibrated hardware setup.

`SIM_VERIFY_MARKS=false` selects the previous unverified motion executor for comparison. This disables automatic checks; it does not turn motion estimates into confirmed ink. These settings apply only to MuJoCo. The SVG-only executor is unchanged.

## Isolated experiment

Run from the project root with the MuJoCo dependencies installed:

```sh
source .venv/bin/activate
python -m scripts.test_chalk_feedback
```

In the restricted development sandbox, add `--sandbox-heartbeat`. No OpenAI key, speech models, server, or real robot is required. The experiment creates temporary scenes; it does not modify the application's board. JPEG before/after frames, scene snapshots and JSON measurements are written under `artifacts/chalk-feedback/` (override with `--output`). Open `artifacts/chalk-feedback/report.html` to compare all before/after images and outcomes. The script exits unsuccessfully if the expected outcomes do not occur.

## What it tests

Four cases use the existing universal drawing engine and actual OpenArm MuJoCo dynamics:

| Case | Hidden environment setup | Expected response |
| --- | --- | --- |
| Fresh | Nominal chalk length | Detect a new visible stroke |
| Shortened | Chalk tip shortened by 0.6 mm | Detect a missing stroke; try bounded depth corrections until marks appear |
| Exhausted | Tip shortened by 3 mm | Detect missing marks; stop after the correction budget |
| Wearing | Tip shortens as it deposits marks | Detect an incomplete stroke; stop for inspection |

Environment setup shifts the physical contact nib along the chalk axis. The controller's nominal tool site stays unchanged. Wear rate is proportional to deposited path length, capped at 10 mm loss. This is a simplified geometric contact-loss experiment, not a model of chalk dust, fracture, variable abrasion or pressure. Rendered marks remain dark on a light surface; white chalk on a real tabletop needs its own validation.

The checking loop parks the arm, takes a baseline image, queues a short line through `DrawingEngine.draw_strokes`, finishes the motion, parks again, and takes a fresh image. `backend/mark_verification.py` receives only those two images, the intended path and a fixed camera calibration. It cannot access joint contacts, environment ink, or the hidden shortening amount. The report includes environment labels for evaluation, but the decision policy never receives them.

The fixed simulated camera maps the normalized board onto approximately pixel corners (166.6, 86.6) and (473.4, 393.4) in a 640×480 image. This calibration is specific to the present overhead view. It must be replaced by an undistorted/rectified real-camera mapping if the camera or table changes.

Image comparison checks a narrow region around the intended path, removes a small global exposure shift, and measures changed-pixel coverage. A visible result requires at least 95% path coverage, sufficient changed pixels, and contrast. Partial/faint marks stop for inspection. Large scene changes, substantial exposure changes, occlusion, and significant pre-existing ink on the path produce an inconclusive result. This is a simple controlled-scene detector: matching pixels do not prove causality, contact pressure, or correct chalk mechanics. An unrelated change directly on the intended path can still fool it; this is not a validated hardware controller.

## Bounded simulation retries

Only a **missing** result permits retry: lower the nominal drawing height by 0.25 mm, at most three times, with a 0.75 mm total experiment limit. Fresh stroke IDs preserve all attempts and existing marks. Partial/faint or inconclusive results never automatically lower the tool. The low-level simulation setter additionally rejects offsets outside 0–1 mm.

These numerical bounds are test parameters, not recommended hardware limits. The normal MuJoCo app now runs the same bounded strategy through `VerifiedMuJoCoExecutor`; the standalone script remains isolated. Astra has no depth-control tool. Integrating it with real hardware needs calibrated limits, an independent stop mechanism, realistic image testing and a way to distinguish failed contact from broken chalk or poor visibility.

## Automated tests

```sh
python -m pytest -q
```

Tests cover visible/missing/partial/faint marks, old ink, occlusion, exposure shifts, unrelated marks, calibration input validation and retry limits. Actual-MuJoCo tests verify that shortening changes only the physical nib, not the controller's nominal tip estimate, and that correction limits reject invalid values. The separate four-case experiment exercises the complete motion → camera → decision → bounded retry loop and records timings. Timings include simulation stepping, persistence, parking and rendering; they are not hardware latency guarantees.

The isolated single-line experiment retains its conservative stop-on-partial policy for comparison. The live app checks after each stroke, immediately prioritizes missing-stroke retries, and can additionally repair clearly localized gaps. It moves to the observation pose when the normal lifted view is inconclusive.

For a faster visible failure/recovery experiment, run `python -m scripts.run_mujoco --chalk-wear-rate 0.02`. The CLI flag overrides an exported environment setting, including an old zero value. Startup prints the selected rate. The implementation shifts the physical contact nib; it does not shorten the decorative marker barrel or simulate dust/crumbling.
