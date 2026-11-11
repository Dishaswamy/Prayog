"""Render every D3/*.md to PDF: markdown -> HTML with print CSS -> headless Chrome --print-to-pdf."""
import re, subprocess, sys
from pathlib import Path
import markdown

D3 = Path(__file__).resolve().parents[1]
OUT = D3 / "pdf"; OUT.mkdir(exist_ok=True)
BUILD = D3 / "build"; BUILD.mkdir(exist_ok=True)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CSS = """
@page { size: A4; margin: 16mm 14mm 16mm 14mm; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 9.6pt; line-height: 1.42; color: #1d1d1f; }
header.cover { border-bottom: 2px solid #2f6db5; margin-bottom: 14px; padding-bottom: 8px; }
header.cover h1 { font-size: 20pt; margin: 0 0 4px; color: #17324d; }
header.cover p { margin: 0; color: #555; font-size: 10pt; }
h1 { font-size: 14pt; color: #17324d; border-bottom: 1px solid #d0d7e2; padding-bottom: 3px; margin-top: 18px; break-after: avoid; }
h2 { font-size: 11.5pt; color: #17324d; margin-top: 14px; break-after: avoid; }
h3 { font-size: 10.5pt; break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 10px; font-size: 8.2pt; break-inside: auto; }
tr { break-inside: avoid; }
th { background: #eef2f8; text-align: left; }
th, td { border: 1px solid #cfd6e0; padding: 3px 5px; vertical-align: top; overflow-wrap: break-word; }
code { font-family: Consolas, "Courier New", monospace; font-size: 8.3pt; background: #f3f4f6; padding: 0 2px; border-radius: 2px; }
pre { background: #f6f7f9; border: 1px solid #e1e4e8; padding: 6px 8px; font-size: 7.9pt; line-height: 1.3; white-space: pre-wrap; break-inside: avoid; }
pre code { background: none; padding: 0; }
img { max-width: 100%; display: block; margin: 6px auto; }
blockquote { border-left: 3px solid #2f6db5; margin: 6px 0; padding: 2px 10px; color: #444; }
.wide table { font-size: 7.2pt; }
.wide td code { font-size: 6.6pt; }
"""

def render(md_path: Path) -> Path:
    text = md_path.read_text(encoding="utf-8")
    title, subtitle = md_path.stem, ""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            v = v.strip().strip('"')
            if k.strip() == "title": title = v
            if k.strip() == "subtitle": subtitle = v
        text = text[m.end():]
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "attr_list"])
    wide = " class=\"wide\"" if "Traceability" in title or "Defect" in title else ""
    landscape = "@page { size: A4 landscape; }" if "Traceability" in title else ""
    html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title><style>{CSS}{landscape}</style></head>"
            f"<body{wide}><header class='cover'><h1>{title}</h1><p>{subtitle}</p></header>{body}</body></html>")
    page = BUILD / (md_path.stem + ".html")
    page.write_text(html.replace('src="figures/', f'src="{(D3 / "figures").as_uri()}/'), encoding="utf-8")
    pdf = OUT / (md_path.stem + ".pdf")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", page.as_uri()], check=True, capture_output=True, timeout=120)
    return pdf

if __name__ == "__main__":
    targets = [D3 / a for a in sys.argv[1:]] or sorted(D3.glob("[0-9][0-9]_*.md"))
    for t in targets:
        print(render(t))
