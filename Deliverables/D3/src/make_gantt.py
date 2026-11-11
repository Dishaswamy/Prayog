"""Gantt chart for Prayog: planned bars (from the sprint plan) against actual bars (from git history)."""
from datetime import date
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

D = date
# (workstream, planned start, planned end, actual start, actual end or None, owner)
ROWS = [
    ("Requirements (SRS, Integrated Spec)", D(2026, 9, 1), D(2026, 9, 23), D(2026, 9, 1), D(2026, 9, 26), "All"),
    ("Architecture (SADD v1.1 / v2.0)", D(2026, 9, 14), D(2026, 9, 26), D(2026, 9, 15), D(2026, 9, 26), "All"),
    ("Backlog, sprint plan, Jira setup", D(2026, 9, 26), D(2026, 9, 28), D(2026, 9, 27), D(2026, 10, 11), "Nideesh, Disha"),
    ("Platform: DB schema, CI, config", D(2026, 9, 28), D(2026, 10, 6), D(2026, 9, 26), D(2026, 10, 12), "Disha"),
    ("Identity and access", D(2026, 9, 28), D(2026, 10, 9), D(2026, 10, 2), D(2026, 10, 19), "Disha, Aditi"),
    ("GPU scheduling core", D(2026, 9, 28), D(2026, 10, 15), D(2026, 10, 13), D(2026, 11, 2), "Adithya"),
    ("Node agent and execution", D(2026, 10, 5), D(2026, 10, 22), D(2026, 10, 15), D(2026, 10, 22), "Adithya, Aditi"),
    ("Experiment ledger and reproduction", D(2026, 10, 5), D(2026, 10, 29), D(2026, 10, 12), D(2026, 10, 19), "Nideesh"),
    ("Steward agentic AI (S1-S4)", D(2026, 10, 1), D(2026, 10, 29), D(2026, 10, 5), D(2026, 11, 2), "Areen"),
    ("CLI client", D(2026, 10, 12), D(2026, 10, 22), D(2026, 10, 19), D(2026, 10, 22), "Disha"),
    ("Web application", D(2026, 10, 26), D(2026, 11, 2), D(2026, 10, 29), D(2026, 11, 5), "Areen"),
    ("Testing, security, performance (D3)", D(2026, 10, 16), D(2026, 11, 6), D(2026, 10, 29), D(2026, 11, 6), "All"),
    ("Demonstration rehearsal", D(2026, 11, 7), D(2026, 11, 13), None, None, "All"),
]
MILESTONES = [("D1", D(2026, 9, 26)), ("D1 viva", D(2026, 10, 10)), ("D2", D(2026, 10, 15)),
              ("D3", D(2026, 11, 6)), ("Demo", D(2026, 11, 13))]
SPRINTS = [("Sprint 1", D(2026, 9, 28), D(2026, 10, 15)), ("Sprint 2", D(2026, 10, 16), D(2026, 11, 6)),
           ("Sprint 3", D(2026, 11, 7), D(2026, 11, 13))]
TODAY = D(2026, 11, 6)

fig, ax = plt.subplots(figsize=(13, 6.8))
for i, (name, ps, pe, a_s, a_e, owner) in enumerate(ROWS):
    y = len(ROWS) - i
    ax.barh(y + 0.18, (pe - ps).days or 1, left=ps, height=0.32, color="#c9d6ea", edgecolor="#5b7bb2", label="Planned" if i == 0 else None)
    if a_s:
        ax.barh(y - 0.18, (a_e - a_s).days or 1, left=a_s, height=0.32, color="#2f6db5", label="Actual (git / documents)" if i == 0 else None)
    ax.text(D(2026, 11, 14), y, owner, va="center", fontsize=8, color="#444")
ax.set_yticks(range(len(ROWS), 0, -1), [r[0] for r in ROWS], fontsize=9)
for name, s, e in SPRINTS:
    ax.axvspan(s, e, color="#f3f3f3" if name != "Sprint 2" else "#eaeaea", zorder=0)
    ax.text(s, len(ROWS) + 0.9, name, fontsize=8, color="#666")
for name, d in MILESTONES:
    ax.axvline(d, color="#c0392b", lw=1, ls="--")
    ax.text(d, 0.15, name, rotation=90, fontsize=8, color="#c0392b", ha="right", va="bottom")
ax.axvline(TODAY, color="#27ae60", lw=1.5)
ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
ax.set_xlim(D(2026, 8, 28), D(2026, 11, 22))
ax.set_ylim(0, len(ROWS) + 1.4)
plt.setp(ax.get_xticklabels(), rotation=45, fontsize=8)
ax.legend(loc="lower left", fontsize=8)
ax.set_title("Prayog timeline: planned vs actual (green line = 6 Nov 2026)", fontsize=11)
ax.grid(axis="x", color="#ddd", lw=0.5)
fig.tight_layout()
fig.savefig(__file__.replace("src\\make_gantt.py", "figures\\gantt.png").replace("src/make_gantt.py", "figures/gantt.png"), dpi=160)
print("ok")
