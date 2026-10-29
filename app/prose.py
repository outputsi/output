"""Render the small Markdown subset used by Output's pages and person summaries."""
import os
import re
from html import escape

PAGES_DIR = os.path.join(os.path.dirname(__file__), "pages")
SUMMARY_DIR = os.path.join(os.path.dirname(__file__), "summaries")

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_PAGE_NAME = re.compile(r"[a-z0-9-]+\.md")
_SLUG = re.compile(r"[a-z0-9-]{1,80}")


def _safe_url(url):
    if url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    if url.startswith("mailto:") and re.fullmatch(r"mailto:[^@\s]+@[^@\s]+\.[^@\s]+", url):
        return url
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return None


def _inline(text):
    """Escape, then links, bold and italic. Only http(s), mailto and site paths become links."""
    text = escape(text)

    def link(match):
        url = _safe_url(match.group(2))
        if not url:
            return match.group(0)
        extra = ' target="_blank" rel="noopener"' if url.startswith(("http://", "https://")) else ""
        return f'<a href="{url}"{extra}>{match.group(1)}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return text


def _kind(line):
    s = line.strip()
    if s in ("---", "***"):
        return "hr"
    if s.startswith("- "):
        return "li"
    if s.startswith("#"):
        return "h"
    if s.startswith(">"):
        return "q"
    return "p"


def _heading(line):
    s = line.strip()
    hashes = len(s) - len(s.lstrip("#"))
    level = 2 if hashes <= 2 else 3
    return f"<h{level}>{_inline(s[hashes:].strip())}</h{level}>"


def _render_run(kind, lines):
    if kind == "hr":
        return "<hr>"
    if kind == "h":
        return "".join(_heading(line) for line in lines)
    if kind == "q":
        text = " ".join(line.strip()[1:].strip() for line in lines)
        return f"<blockquote><p>{_inline(text)}</p></blockquote>"
    if kind == "li":
        items = "".join(f"<li>{_inline(line.strip()[2:].strip())}</li>" for line in lines)
        return f"<ul>{items}</ul>"
    return f"<p>{_inline(' '.join(line.strip() for line in lines))}</p>"


def _render_block(lines):
    runs = []
    for line in lines:
        kind = _kind(line)
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(line)
        else:
            runs.append((kind, [line]))
    return "".join(_render_run(kind, group) for kind, group in runs)


def render_markdown(src):
    """HTML for the headings, lists, quotes, links and emphasis these pages use."""
    html = []
    block = []

    def flush():
        if block:
            html.append(_render_block(block))
            block.clear()

    for line in src.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip():
            block.append(line)
        else:
            flush()
    flush()
    return "\n".join(html)


def page_body(name):
    if not _PAGE_NAME.fullmatch(name):
        raise ValueError(name)
    with open(os.path.join(PAGES_DIR, name), encoding="utf-8") as f:
        return render_markdown(f.read())


def summary_section(person_id):
    """Approved summary for a person, or '' when this person has none."""
    if not _SLUG.fullmatch(person_id or ""):
        return ""
    path = os.path.join(SUMMARY_DIR, person_id + ".md")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        text = f.read().lstrip()
    if text.startswith("# "):
        text = text.split("\n", 1)[1].lstrip("\n")
    body = render_markdown(text)
    if not body:
        return ""
    return f"<section class='summary'>{body}</section>"
