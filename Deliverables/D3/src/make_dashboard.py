"""Project dashboard figure from the Jira export, the backlog plan, the D3 bug list and git history."""
import csv, collections, subprocess, sys
from datetime import date, datetime
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

D3 = Path(__file__).resolve().parents[1]
JIRA = Path(sys.argv[1]) if len(sys.argv) > 1 else D3.parent / "Jira.csv"
plan = list(csv.DictReader(open(D3.parent / "PRAYOG_backlog_jira_import.csv", encoding="utf-8-sig")))
jira = {r["Issue key"]: r for r in csv.DictReader(open(JIRA, encoding="utf-8-sig", errors="replace"))}
bugs = list(csv.DictReader(open(D3 / "evidence" / "jira_bugs_import.csv", encoding="utf-8")))

status = collections.Counter(r["Status"] for r in jira.values() if r["Issue Type"] != "Epic")
sprint = collections.defaultdict(lambda: [0, 0]); epic = collections.defaultdict(lambda: [0, 0]); who = collections.defaultdict(lambda: [0, 0])
for p in plan:
    if p["Issue Type"] == "Epic":
        continue
    pts = float(p["Story Points"] or 0)
    j = jira.get(f"PRAYOG-{int(p['Issue Key'].split('-')[1]) + 4}", {})
    done = j.get("Status") == "Done"
    for d, k in ((sprint, p["Sprint"] or "Backlog"), (epic, p["Component"].replace("\ufffd", "-")), (who, j.get("Assignee") or "(unassigned)")):
        d[k][0] += pts; d[k][1] += pts if done else 0

log = subprocess.run(["git", "-C", r"C:/Users/Areen/Prayog-final", "log", "origin/main", "--no-merges", "--format=%ad", "--date=short"],
                     capture_output=True, text=True).stdout.split()
weeks = collections.Counter(datetime.strptime(d, "%Y-%m-%d").date().isocalendar()[1] for d in log)

fig, ax = plt.subplots(2, 3, figsize=(15, 8.2))
a = ax[0][0]; labels = list(status); a.pie([status[k] for k in labels], labels=[f"{k} ({status[k]})" for k in labels],
                                          colors=[{"Done": "#2f6db5", "In Progress": "#f0b429", "To Do": "#c9d6ea"}.get(k, "#999") for k in labels], startangle=90)
a.set_title(f"Issues by status (no epics, n={sum(status.values())})", fontsize=10)
a = ax[0][1]; ks = ["S1", "S2", "S3", "Backlog"]
a.bar([k for k in ks], [sprint[k][0] for k in ks], color="#c9d6ea", label="committed")
a.bar([k for k in ks], [sprint[k][1] for k in ks], color="#2f6db5", width=0.5, label="done")
for i, k in enumerate(ks): a.text(i, sprint[k][0] + 2, f"{sprint[k][1]:.0f}/{sprint[k][0]:.0f}", ha="center", fontsize=8)
a.set_title("Story points per sprint (velocity)", fontsize=10); a.legend(fontsize=8)
a = ax[0][2]; ek = sorted(epic, key=lambda k: -epic[k][0])
a.barh(ek, [epic[k][0] for k in ek], color="#c9d6ea"); a.barh(ek, [epic[k][1] for k in ek], color="#2f6db5", height=0.5)
a.invert_yaxis(); a.tick_params(labelsize=7); a.set_title("Points per epic (done in blue)", fontsize=10)
a = ax[1][0]; wk = sorted(who, key=lambda k: -who[k][0])
a.bar(wk, [who[k][0] for k in wk], color="#c9d6ea"); a.bar(wk, [who[k][1] for k in wk], color="#2f6db5", width=0.5)
a.tick_params(axis="x", labelsize=7, rotation=20); a.set_title("Points per assignee (Jira)", fontsize=10)
a = ax[1][1]; bs = collections.Counter((b["Status"], b["Priority"]) for b in bugs)
pr = ["Highest", "High", "Medium", "Low"]
done = [sum(v for (s, p), v in bs.items() if s == "Done" and p == x) for x in pr]
open_ = [sum(v for (s, p), v in bs.items() if s != "Done" and p == x) for x in pr]
a.bar(pr, done, color="#2f6db5", label="fixed"); a.bar(pr, open_, bottom=done, color="#c0392b", label="open")
a.set_title(f"D3 defects by priority (n={len(bugs)})", fontsize=10); a.legend(fontsize=8)
a = ax[1][2]; wks = sorted(weeks)
a.bar([f"W{w}" for w in wks], [weeks[w] for w in wks], color="#2f6db5"); a.tick_params(axis="x", labelsize=7, rotation=45)
a.set_title(f"Commits on main per ISO week (n={len(log)})", fontsize=10)
fig.suptitle(f"Prayog project dashboard — Jira export {JIRA.name}, git main, D3 defect register", fontsize=11)
fig.tight_layout(); fig.savefig(D3 / "figures" / "dashboard.png", dpi=140)
print(dict(status), {k: v for k, v in sprint.items()})
