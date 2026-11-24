"""Render docs/*.md to docs/pdf/*.pdf (and a combined manual).

    .venv/bin/python scripts/build_docs.py
"""
from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

import markdown
from weasyprint import CSS, HTML

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "pdf"

CSS_TEXT = """
@page {
  size: Letter; margin: 22mm 18mm 20mm 18mm;
  @bottom-left { content: "OtterBench · " string(doctitle); font: 8pt 'DejaVu Sans'; color: #888; }
  @bottom-right { content: counter(page) " / " counter(pages); font: 8pt 'DejaVu Sans'; color: #888; }
}
@page :first { @bottom-left { content: none; } }
html { font: 10pt/1.5 'DejaVu Sans', sans-serif; color: #1d2329; }
.cover { border-bottom: 3px solid #2b6cb0; padding-bottom: 10px; margin-bottom: 18px; }
.cover .kicker { color: #2b6cb0; font-size: 9pt; letter-spacing: 2px; text-transform: uppercase; }
.cover .title { font-size: 24pt; font-weight: bold; margin: 4px 0; string-set: doctitle content(); }
.cover .subtitle { color: #555; font-size: 12pt; }
.cover .date { color: #888; font-size: 8.5pt; margin-top: 6px; }
h1 { display: none; }
h2 { font-size: 14pt; color: #2b6cb0; border-bottom: 1px solid #d5dde6; padding-bottom: 3px;
     margin-top: 22px; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin-top: 16px; page-break-after: avoid; }
h4 { font-size: 10.5pt; page-break-after: avoid; }
p, li { orphans: 3; widows: 3; }
a { color: #2b6cb0; text-decoration: none; }
code { font: 8.8pt 'DejaVu Sans Mono', monospace; background: #f1f4f7; padding: 0 3px;
       border-radius: 3px; }
pre { background: #f1f4f7; border: 1px solid #dfe5eb; border-radius: 4px; padding: 8px 10px;
      font-size: 8.3pt; line-height: 1.35; white-space: pre-wrap; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 12px; font-size: 9pt;
        page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { background: #2b6cb0; color: white; text-align: left; padding: 4px 6px; }
td { border-bottom: 1px solid #dfe5eb; padding: 4px 6px; vertical-align: top; }
tr:nth-child(even) td { background: #f7f9fb; }
blockquote { margin: 10px 0; padding: 6px 12px; background: #fff8e6; border-left: 4px solid #e0a100;
             page-break-inside: avoid; }
blockquote p { margin: 2px 0; }
img { max-width: 100%; border: 1px solid #c9d2db; border-radius: 4px; margin: 6px 0;
      page-break-inside: avoid; }
.toc-page h2 { display: block; }
"""


def parse_front_matter(text: str) -> tuple[dict, str]:
    meta = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        text = text[m.end():]
    return meta, text


def render(md_path: Path) -> tuple[str, str]:
    meta, body = parse_front_matter(md_path.read_text())
    html = markdown.markdown(body, extensions=["tables", "fenced_code", "sane_lists", "attr_list"])
    title = meta.get("title", md_path.stem)
    cover = (f'<div class="cover"><div class="kicker">OtterBench</div>'
             f'<div class="title">{title}</div>'
             f'<div class="subtitle">{meta.get("subtitle", "")}</div>'
             f'<div class="date">Generated {datetime.date.today():%B %d, %Y}</div></div>')
    return title, cover + html


def write_pdf(html_body: str, out: Path):
    doc = f"<html><head><meta charset='utf-8'></head><body>{html_body}</body></html>"
    HTML(string=doc, base_url=str(DOCS)).write_pdf(out, stylesheets=[CSS(string=CSS_TEXT)])


def main():
    OUT.mkdir(exist_ok=True)
    sources = sorted(DOCS.glob("[0-9][0-9]-*.md"))
    combined = []
    for src in sources:
        title, body = render(src)
        out = OUT / (src.stem + ".pdf")
        write_pdf(body, out)
        combined.append(body)
        print(f"  {out.relative_to(ROOT)}")
    manual = OUT / "OtterBench-Manual.pdf"
    write_pdf('<div style="page-break-after: always"></div>'.join(combined), manual)
    print(f"  {manual.relative_to(ROOT)}  (all guides combined)")


if __name__ == "__main__":
    sys.exit(main())
