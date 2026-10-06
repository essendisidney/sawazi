"""Inject demo_output.json into the dashboard template -> sawazi_demo.html"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
d = json.loads((ROOT / "demo_output.json").read_text())
keep = {k: d[k] for k in ("matching", "accuracy", "portfolio_before", "portfolio_after", "checkoff",
                          "exceptions", "reminders", "match_methods")}
keep["accuracy"] = {k: v for k, v in keep["accuracy"].items() if k != "wrong"}
html = (ROOT / "scripts" / "dashboard_template.html").read_text()
html = html.replace("/*DATA*/null", json.dumps(keep, separators=(",", ":")).replace("</", "<\\/"))
(ROOT / "sawazi_demo.html").write_text(html)
print("written", len(html))
