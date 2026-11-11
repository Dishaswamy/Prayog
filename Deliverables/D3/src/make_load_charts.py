"""Charts and a markdown table from the baseline and optimised load-test JSON files."""
import json, sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

EV = Path(__file__).resolve().parents[1] / "evidence"
FIG = Path(__file__).resolve().parents[1] / "figures"
base = json.loads((EV / "load_baseline.json").read_text())["results"]
opt = json.loads((EV / "load_optimized.json").read_text())["results"]
key = lambda r: (r["scenario"], r["concurrency"])
B, O = {key(r): r for r in base}, {key(r): r for r in opt}
scen = list(dict.fromkeys(r["scenario"] for r in base))

fig, axes = plt.subplots(2, len(scen), figsize=(3.2 * len(scen), 6.2))
for j, s in enumerate(scen):
    cs = sorted(c for (n, c) in B if n == s)
    for row, (metric, label) in enumerate((("rps", "throughput (req/s)"), ("p95_ms", "p95 latency (ms, log)"))):
        ax = axes[row][j]
        ax.plot(cs, [B[(s, c)][metric] for c in cs], "o-", color="#c0392b", label="baseline (main 18767aa)")
        ax.plot(cs, [O[(s, c)][metric] for c in cs if (s, c) in O], "s-", color="#2f6db5", label="optimised (d3-testing)")
        ax.set_xscale("log", base=2); ax.set_xticks(cs, [str(c) for c in cs])
        if metric == "p95_ms": ax.set_yscale("log")
        ax.grid(color="#ddd", lw=0.5)
        if row == 0: ax.set_title(s, fontsize=9)
        if j == 0: ax.set_ylabel(label, fontsize=8)
        if row == 1: ax.set_xlabel("concurrent virtual users", fontsize=8)
        ax.tick_params(labelsize=7)
axes[0][0].legend(fontsize=7)
fig.tight_layout(); fig.savefig(FIG / "load_comparison.png", dpi=150)

lines = ["| Scenario | VUs | Baseline req/s | Optimised req/s | Baseline p50 / p95 (ms) | Optimised p50 / p95 (ms) | Baseline errors | Optimised errors |",
         "|---|---|---|---|---|---|---|---|"]
for s in scen:
    for c in sorted(c for (n, c) in B if n == s):
        b, o = B[(s, c)], O.get((s, c))
        err = lambda r: f"{r['error_rate']*100:.1f}% " + ("" if r["error_rate"] == 0 else "(" + ", ".join(f"{k}×{v}" for k, v in r["codes"].items() if not k.startswith("2")) + ")")
        lines.append(f"| {s} | {c} | {b['rps']} | {o['rps'] if o else '—'} | {b['p50_ms']:.0f} / {b['p95_ms']:.0f} | "
                     f"{(str(round(o['p50_ms'])) + ' / ' + str(round(o['p95_ms']))) if o else '—'} | {err(b)} | {err(o) if o else '—'} |")
(EV / "load_table.md").write_text("\n".join(lines))
print("\n".join(lines))
