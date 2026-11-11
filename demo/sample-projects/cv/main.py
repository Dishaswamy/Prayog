"""CV (segmentation) starter for Prayog.

Thresholds synthetic images that each contain one bright square, and reports the
IoU of the predicted mask. Replace this with your real model; keep reading
inputs from the PRAYOG_* variables and writing metrics/checkpoints the same way.
Deterministic (fixed seed), so a rebuild should reproduce it exactly.
"""
import json
import os
import pathlib
import random

hp = json.loads(os.environ.get("PRAYOG_HYPERPARAMETERS_JSON", "{}"))
size = int(hp.get("size", 32))
steps = int(hp.get("steps", 5))
seed = int(hp.get("seed", 0))

out = pathlib.Path(os.environ.get("PRAYOG_OUTPUT_DIR", "output"))
(out / "checkpoints").mkdir(parents=True, exist_ok=True)
rng = random.Random(seed)


def sample():
    """A noisy image with one bright square, and its true mask."""
    x0, y0 = rng.randrange(size // 2), rng.randrange(size // 2)
    side = rng.randrange(4, size // 2)
    mask = [[x0 <= c < x0 + side and y0 <= r < y0 + side for c in range(size)] for r in range(size)]
    image = [[(0.8 if mask[r][c] else 0.2) + rng.uniform(-0.25, 0.25) for c in range(size)] for r in range(size)]
    return image, mask


def iou(pred, true):
    inter = sum(p and t for pr, tr in zip(pred, true) for p, t in zip(pr, tr))
    union = sum(p or t for pr, tr in zip(pred, true) for p, t in zip(pr, tr))
    return inter / union if union else 1.0


threshold = 0.9   # starts too high; each step nudges it towards the best value
best = None
for step in range(1, steps + 1):
    scores = []
    for _ in range(8):
        image, mask = sample()
        scores.append(iou([[v > threshold for v in row] for row in image], mask))
    score = sum(scores) / len(scores)
    print(f"step {step}: threshold={threshold:.3f} iou={score:.4f}")

    with open(out / "metrics.jsonl", "a") as m:
        m.write(json.dumps({"name": "iou", "value": score, "step": step}) + "\n")

    is_best = best is None or score > best
    best = score if is_best else best
    name = f"step-{step}-best.json" if is_best else f"step-{step}.json"
    tmp = out / "checkpoints" / f"step-{step}.tmp"
    tmp.write_text(json.dumps({"threshold": threshold, "step": step}))
    tmp.rename(out / "checkpoints" / name)

    threshold -= 0.08
print("done")
