"""Dummy Prayog job: fits y = w*x + b on data/train.csv with plain gradient descent.

Deterministic (no randomness), so a rebuild should reproduce it exactly.
"""
import csv
import json
import os
import pathlib

hp = json.loads(os.environ.get("PRAYOG_HYPERPARAMETERS_JSON", "{}"))
lr = float(hp.get("lr", 0.05))
epochs = int(hp.get("epochs", 10))

data_dir = pathlib.Path(os.environ.get("PRAYOG_DATA_DIR", "data"))
out = pathlib.Path(os.environ.get("PRAYOG_OUTPUT_DIR", "output"))
(out / "checkpoints").mkdir(parents=True, exist_ok=True)

with open(data_dir / "train.csv", newline="") as fh:
    rows = [(float(r["x"]), float(r["y"])) for r in csv.DictReader(fh)]
print(f"loaded {len(rows)} rows, lr={lr}, epochs={epochs}")

w, b = 0.0, 0.0
best = None
for step in range(1, epochs + 1):
    gw = sum(2 * (w * x + b - y) * x for x, y in rows) / len(rows)
    gb = sum(2 * (w * x + b - y) for x, y in rows) / len(rows)
    w, b = w - lr * gw, b - lr * gb
    loss = sum((w * x + b - y) ** 2 for x, y in rows) / len(rows)
    print(f"step {step}: loss={loss:.6f} w={w:.4f} b={b:.4f}")

    with open(out / "metrics.jsonl", "a") as m:
        m.write(json.dumps({"name": "loss", "value": loss, "step": step}) + "\n")

    is_best = best is None or loss < best
    best = loss if is_best else best
    name = f"step-{step}-best.json" if is_best else f"step-{step}.json"
    tmp = out / "checkpoints" / f"step-{step}.tmp"
    tmp.write_text(json.dumps({"w": w, "b": b, "step": step}))
    tmp.rename(out / "checkpoints" / name)

print("done")
