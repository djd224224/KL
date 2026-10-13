#!/usr/bin/env python3
"""Render a short Markdown report as an HTML page for send_html_email.py.

The unattended report jobs (run_cpi_pilot_report.ps1) get claude -p's answer
as Markdown, and Gmail would show a table's raw pipes. No Markdown package is
installed here, so this covers what those reports use: headings, pipe tables
(with :--: alignment), bullet and numbered lists, fenced code, rules and
paragraphs (line breaks kept), with **bold**, *italics* and `code` inline.
Everything else is escaped. Styles are inline because Gmail keeps those; the
body rule in <style> is the one send_html_email.py moves onto a wrapping div.

Usage: python md_report_email.py OUT_HTML TITLE MD_FILE [MD_FILE ...]
       (each later file follows the first below a rule)
"""
import html
import re
import sys

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d{1,3}[.)]\s+(.*)$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")

FONT = "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
MONO = "Consolas, Menlo, monospace"
STYLE = {
    "h1": "font-size:20px;margin:18px 0 8px",
    "h2": "font-size:17px;margin:16px 0 6px",
    "h3": "font-size:15px;margin:14px 0 6px",
    "p": "margin:0 0 10px",
    "list": "margin:0 0 10px;padding-left:22px",
    "li": "margin:2px 0",
    "table": "border-collapse:collapse;margin:4px 0 12px;font-size:13px",
    "th": "border:1px solid #d0d7de;background:#f3f4f6;padding:5px 8px;font-weight:600",
    "td": "border:1px solid #d0d7de;padding:5px 8px",
    "code": f"font-family:{MONO};font-size:12px;background:#f3f4f6;padding:1px 4px",
    "pre": (f"font-family:{MONO};font-size:12px;background:#f3f4f6;padding:8px 10px;"
            "white-space:pre-wrap;margin:0 0 10px"),
    "hr": "border:0;border-top:1px solid #d0d7de;margin:20px 0",
}


def inline(text):
    """One line's inline Markdown, escaped: `code`, **bold**, *italics*."""
    out = []
    for i, part in enumerate(re.split(r"(`[^`]+`)", text)):
        if i % 2:
            out.append(f'<code style="{STYLE["code"]}">'
                       f"{html.escape(part[1:-1], quote=False)}</code>")
            continue
        s = html.escape(part, quote=False)
        s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", s)
        # a lone * between spaces (2 * 3) is arithmetic, not italics
        s = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", s)
        out.append(s)
    return "".join(out)


def _cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT.split(s)]


def _aligns(sep):
    out = []
    for c in _cells(sep):
        left, right = c.startswith(":"), c.endswith(":")
        out.append("center" if left and right else "right" if right else "left")
    return out


def _table(head, aligns, rows):
    n = max([len(head)] + [len(r) for r in rows])

    def tr(cells, tag):
        cells = cells + [""] * (n - len(cells))
        return "<tr>" + "".join(
            f'<{tag} style="{STYLE[tag]};text-align:'
            f'{aligns[i] if i < len(aligns) else "left"}">{inline(c)}</{tag}>'
            for i, c in enumerate(cells)) + "</tr>"
    # the report script's key/value tables have an empty header row
    thead = f"<thead>{tr(head, 'th')}</thead>" if any(head) else ""
    return (f'<table style="{STYLE["table"]}">{thead}'
            f"<tbody>{''.join(tr(r, 'td') for r in rows)}</tbody></table>")


def render(md):
    """Markdown text -> an HTML fragment."""
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, para, items, kind = [], [], [], None

    def flush():
        nonlocal para, items, kind
        if para:
            out.append(f'<p style="{STYLE["p"]}">'
                       + "<br>".join(inline(x) for x in para) + "</p>")
        if items:
            out.append(f'<{kind} style="{STYLE["list"]}">'
                       + "".join(f'<li style="{STYLE["li"]}">{x}</li>' for x in items)
                       + f"</{kind}>")
        para, items, kind = [], [], None

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            flush()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            out.append(f'<pre style="{STYLE["pre"]}">'
                       + html.escape("\n".join(lines[i + 1:j]), quote=False) + "</pre>")
            i = j + 1
            continue
        if (s.startswith("|") and i + 1 < len(lines) and "-" in lines[i + 1]
                and _TABLE_SEP.match(lines[i + 1])):
            flush()
            j, rows = i + 2, []
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(_cells(lines[j]))
                j += 1
            out.append(_table(_cells(s), _aligns(lines[i + 1]), rows))
            i = j
            continue
        heading = _HEADING.match(s)
        item = _BULLET.match(line) or _NUMBERED.match(line)
        if not s:
            flush()
        elif heading:
            flush()
            tag = "h%d" % min(len(heading.group(1)), 3)
            out.append(f'<{tag} style="{STYLE[tag]}">{inline(heading.group(2))}</{tag}>')
        elif _RULE.match(s):
            flush()
            out.append(f'<hr style="{STYLE["hr"]}">')
        elif item:
            k = "ul" if _BULLET.match(line) else "ol"
            if para or (items and k != kind):
                flush()
            kind = k
            items.append(inline(item.group(1)))
        elif items and line[:1].isspace():
            items[-1] += " " + inline(s)            # a wrapped list item
        else:
            if items:
                flush()
            para.append(s)
        i += 1
    flush()
    return "\n".join(out)


def page(title, fragments):
    body = f'\n<hr style="{STYLE["hr"]}">\n'.join(fragments)
    return ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">\n"
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{html.escape(title)}</title>\n"
            f"<style>body {{ font-family: {FONT}; font-size: 14px; line-height: 1.45; "
            "color: #1f2328; background: #ffffff; padding: 12px; }</style>\n"
            f"</head><body>\n{body}\n</body></html>\n")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 3:
        sys.exit("usage: md_report_email.py OUT_HTML TITLE MD_FILE [MD_FILE ...]")
    out, title, paths = args[0], args[1], args[2:]
    parts = []
    for p in paths:
        with open(p, encoding="utf-8-sig", errors="replace") as fh:   # a PowerShell BOM
            parts.append(render(fh.read()))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page(title, parts))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
