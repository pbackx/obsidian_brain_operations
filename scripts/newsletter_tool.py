#!/usr/bin/env python3
"""newsletter_tool.py - Obsidian Markdown -> HTML e-mail -> campaign draft.

Rules followed: zero third-party dependencies (Flask Vibe philosophy: tiny,
flat, explicit), no build step, every external call is a plain HTTP request
through ``requests``.

Two commands:
    render   Convert an Obsidian note into a responsive HTML e-mail file.
    push     Render the note and create a DRAFT campaign at the provider
             (MailerLite, Listmonk, Buttondown, Kit/ConvertKit v4).

Nothing is ever sent: ``push`` always asks the provider for a draft.

Examples
    python3 newsletter_tool.py render \
        --file "Marketing Ideas/Newsletter 2026-12.md" --out /tmp/news.html

    python3 newsletter_tool.py push --provider mailerlite \
        --file "Marketing Ideas/Newsletter 2026-12.md" \
        --subject "December update" --from-name "Peter" \
        --from-email "news@peated.be" --token "$MAILERLITE_TOKEN" \
        --group 123456789 --dry-run

Obsidian syntax handled: YAML frontmatter (stripped), ``[[Note|Alias]]`` /
``[[Note]]`` wikilinks, ``![[image.png]]`` embeds, ``> [!note]`` callouts,
``%%comments%%``, ``==highlight==``, GFM tables, fenced code, task lists.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from typing import Iterable

# --------------------------------------------------------------------------
# Inline CSS. E-mail clients ignore <style> more often than not, so the
# renderer inlines the essentials and only keeps a small <style> block for
# dark mode / mobile tweaks.
# --------------------------------------------------------------------------
STYLE = {
    "body": "margin:0;padding:0;background:#f6f5f2;",
    "wrapper": (
        "max-width:640px;margin:0 auto;padding:28px 24px;"
        "background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:17px;"
        "line-height:1.6;color:#1f2328;"
    ),
    "h1": "font-size:27px;line-height:1.25;margin:0 0 18px;color:#111418;",
    "h2": "font-size:22px;line-height:1.3;margin:30px 0 12px;color:#111418;",
    "h3": "font-size:19px;line-height:1.3;margin:24px 0 10px;color:#111418;",
    "p": "margin:0 0 16px;",
    "a": "color:#0b62d6;text-decoration:underline;",
    "ul": "margin:0 0 16px;padding-left:22px;",
    "ol": "margin:0 0 16px;padding-left:22px;",
    "li": "margin:0 0 8px;",
    "blockquote": (
        "margin:0 0 16px;padding:12px 18px;border-left:4px solid #b6c2cf;"
        "background:#f4f6f8;color:#3d4550;font-style:normal;"
    ),
    "code": (
        "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
        "font-size:14px;background:#eef1f4;padding:2px 5px;border-radius:4px;"
    ),
    "pre": (
        "margin:0 0 16px;padding:14px 16px;background:#11171f;color:#e6edf3;"
        "border-radius:8px;overflow-x:auto;font-size:14px;line-height:1.45;"
    ),
    "table": "border-collapse:collapse;width:100%;margin:0 0 18px;font-size:15px;",
    "th": (
        "border:1px solid #d6dde4;background:#f0f3f6;padding:9px 11px;"
        "text-align:left;font-weight:600;"
    ),
    "td": "border:1px solid #d6dde4;padding:9px 11px;vertical-align:top;",
    "hr": "border:0;border-top:1px solid #dde3e9;margin:26px 0;",
    "img": "max-width:100%;height:auto;border:0;display:block;margin:0 0 16px;",
    "footer": (
        "max-width:640px;margin:0 auto;padding:14px 24px 30px;font-size:13px;"
        "line-height:1.5;color:#6a737d;font-family:-apple-system,"
        "BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    ),
}


def _st(tag: str, extra: str = "") -> str:
    """Return a ``style="..."`` attribute for a tag, optionally extended."""
    base = STYLE.get(tag, "")
    css = f"{base} {extra}".strip()
    return f' style="{css}"' if css else ""


# --------------------------------------------------------------------------
# Obsidian pre-processing
# --------------------------------------------------------------------------
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
COMMENT_RE = re.compile(r"%%.*?%%", re.S)
CALLOUT_RE = re.compile(r"^>\s*\[!(\w+)\][+-]?\s*(.*)$")


def strip_frontmatter(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1)


def obsidian_to_markdown(text: str) -> str:
    """Normalise Obsidian-only syntax into portable Markdown."""
    text = COMMENT_RE.sub("", text)

    # ![[file.png|Alt text]] -> image, [[Note|Alias]] -> plain link
    def _embed(m: re.Match) -> str:
        target = m.group(1).strip()
        alt = (m.group(2) or "").strip() or os.path.basename(target)
        # Vault paths contain spaces; e-mail clients need them percent-encoded.
        url = target.replace(" ", "%20")
        if re.search(r"\.(png|jpe?g|gif|webp|svg)$", target, re.I):
            return f"![{alt}]({url})"
        return f"[{alt}]({url})"

    text = re.sub(r"!?\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", _embed, text)
    text = re.sub(r"==(.+?)==", r"**\1**", text)  # highlight -> bold
    return text


# --------------------------------------------------------------------------
# Block-level Markdown -> HTML
# --------------------------------------------------------------------------
def _inline(text: str) -> str:
    """Inline spans: escape first, then apply emphasis/links/code."""
    placeholders: list[str] = []

    def _hold(fragment: str) -> str:
        placeholders.append(fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    # Inline code is protected before escaping so its content stays literal.
    text = re.sub(
        r"`([^`]+)`",
        lambda m: _hold(f"<code{_st('code')}>{html.escape(m.group(1))}</code>"),
        text,
    )
    text = html.escape(text, quote=False)
    # Images then links (images first: the syntax is a superset).
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)\)",
        lambda m: _hold(
            f'<img src="{html.escape(m.group(2), quote=True)}" '
            f'alt="{html.escape(m.group(1), quote=True)}"{_st("img")}>'
        ),
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: _hold(
            f'<a href="{html.escape(m.group(2), quote=True)}"{_st("a")}>'
            f"{m.group(1)}</a>"
        ),
        text,
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)
    text = re.sub(r" {2,}\n", "<br>\n", text)

    def _restore(m: re.Match) -> str:
        return placeholders[int(m.group(1))]

    return re.sub(r"\x00(\d+)\x00", _restore, text)


def _table(rows: list[str]) -> str:
    def cells(line: str) -> list[str]:
        line = line.strip().strip("|")
        return [c.strip() for c in re.split(r"(?<!\\)\|", line)]

    header = cells(rows[0])
    out = [f"<table{_st('table')}>", "<thead><tr>"]
    out += [f"<th{_st('th')}>{_inline(c)}</th>" for c in header]
    out.append("</tr></thead><tbody>")
    for row in rows[2:]:
        out.append("<tr>")
        out += [f"<td{_st('td')}>{_inline(c)}</td>" for c in cells(row)]
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def md_to_html(text: str) -> str:
    """Convert Markdown to inline-styled HTML e-mail body."""
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    list_stack: list[str] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            joined = "\n".join(para).strip()
            if joined:
                out.append(f"<p{_st('p')}>{_inline(joined)}</p>")
            para.clear()

    def close_lists(to_len: int = 0) -> None:
        while len(list_stack) > to_len:
            out.append(f"</{list_stack.pop()}>")

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code
        if stripped.startswith("```"):
            flush_para()
            close_lists()
            lang = stripped[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(
                f"<pre{_st('pre')}><code>"
                f"{html.escape(chr(10).join(buf))}</code></pre>"
            )
            _ = lang
            continue

        # blank line
        if not stripped:
            flush_para()
            # A blank line closes lists unless the next block is list content.
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            nxt = lines[j].strip() if j < n else ""
            if not re.match(r"^([-*+]|\d+[.)])\s", nxt):
                close_lists()
            i += 1
            continue

        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            close_lists()
            level = min(len(m.group(1)), 3)
            out.append(f"<h{level}{_st(f'h{level}')}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # horizontal rule
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_para()
            close_lists()
            out.append(f"<hr{_st('hr')}>")
            i += 1
            continue

        # table
        if "|" in stripped and i + 1 < n and re.match(
            r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]
        ):
            flush_para()
            close_lists()
            block = [lines[i], lines[i + 1]]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                block.append(lines[i])
                i += 1
            out.append(_table(block))
            continue

        # callout / blockquote
        if stripped.startswith(">"):
            flush_para()
            close_lists()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                raw = lines[i].strip()[1:].strip()
                cm = CALLOUT_RE.match(lines[i].strip())
                if cm:
                    kind, title = cm.group(1).lower(), cm.group(2).strip()
                    label = title or kind.capitalize()
                    icon = {
                        "warning": "⚠️",
                        "caution": "⚠️",
                        "danger": "🚨",
                        "tip": "💡",
                        "note": "📝",
                        "info": "ℹ️",
                        "success": "✅",
                        "question": "❓",
                        "example": "🧪",
                        "quote": "❝",
                    }.get(kind, "📝")
                    buf.append(f"**{icon} {label}**")
                    _ = raw
                elif raw:
                    buf.append(raw)
                i += 1
            inner = "<br>".join(_inline(b) for b in buf)
            out.append(f"<blockquote{_st('blockquote')}>{inner}</blockquote>")
            continue

        # lists
        lm = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if lm:
            flush_para()
            indent = len(lm.group(1).replace("\t", "    "))
            ordered = bool(re.match(r"\d", lm.group(2)))
            depth = indent // 2 + 1
            while len(list_stack) > depth:
                out.append(f"</{list_stack.pop()}>")
            if len(list_stack) == depth:
                want = "ol" if ordered else "ul"
                if list_stack[-1] != want:
                    out.append(f"</{list_stack.pop()}>")
                    out.append(f"<{want}{_st(want)}>")
                    list_stack.append(want)
                elif list_stack[-1] != want:
                    pass
            else:
                want = "ol" if ordered else "ul"
                out.append(f"<{want}{_st(want)}>")
                list_stack.append(want)
            item = lm.group(3)
            tm = re.match(r"^\[( |x|X)\]\s+(.*)$", item)
            if tm:
                mark = "☑" if tm.group(1).lower() == "x" else "☐"
                item = f"{mark} {tm.group(2)}"
            out.append(f"<li{_st('li')}>{_inline(item)}</li>")
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    close_lists()
    return "\n".join(out)


# --------------------------------------------------------------------------
# Document wrapper
# --------------------------------------------------------------------------
def render_email(
    markdown_text: str,
    subject: str = "",
    preheader: str = "",
    footer_note: str = "You are receiving this e-mail because you subscribed.",
    unsubscribe_url: str = "{{unsubscribe_url}}",
) -> str:
    body = md_to_html(obsidian_to_markdown(strip_frontmatter(markdown_text)))
    pre = (
        f'<div style="display:none;font-size:1px;color:#f6f5f2;'
        f'line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">'
        f"{html.escape(preheader)}</div>"
        if preheader
        else ""
    )
    footer = (
        f'<div{_st("footer")}>{html.escape(footer_note)}<br>'
        f'<a href="{html.escape(unsubscribe_url, quote=True)}"{_st("a")}>'
        f"Unsubscribe</a></div>"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(subject)}</title>"
        "<style>@media (max-width:600px){"
        "table,pre{font-size:15px!important;}"
        "h1{font-size:24px!important;}}</style></head>\n"
        f'<body style="{STYLE["body"]}">{pre}\n'
        f'<div{_st("wrapper")}>\n{body}\n</div>\n{footer}\n</body></html>\n'
    )


# --------------------------------------------------------------------------
# Providers - every "push" creates a DRAFT only.
# --------------------------------------------------------------------------
class PushError(RuntimeError):
    pass


def _post(url: str, headers: dict, payload: dict) -> dict:
    import requests

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise PushError(f"{url} -> HTTP {resp.status_code}: {resp.text[:400]}")
    try:
        return resp.json()
    except ValueError:
        return {}


def push_mailerlite(html_body: str, args) -> dict:
    payload = {
        "name": args.name or args.subject,
        "type": "regular",
        "emails": [
            {
                "subject": args.subject,
                "from": args.from_email,
                "from_name": args.from_name,
                "content": html_body,
            }
        ],
    }
    if args.group:
        payload["groups"] = [str(g).strip() for g in args.group.split(",")]
    data = _post(
        "https://connect.mailerlite.com/api/campaigns",
        {
            "Authorization": f"Bearer {args.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        payload,
    )
    cid = (data.get("data") or {}).get("id")
    return {
        "provider": "mailerlite",
        "campaign_id": cid,
        "draft_url": f"https://dashboard.mailerlite.com/campaigns/{cid}" if cid else "",
        "status": "draft",
    }


def push_listmonk(html_body: str, args) -> dict:
    if not args.list_id:
        raise PushError("--list-id is required for Listmonk")
    payload = {
        "name": args.name or args.subject,
        "subject": args.subject,
        "lists": [int(x) for x in str(args.list_id).split(",")],
        "type": "regular",
        "content_type": "html",
        "body": html_body,
        "from_email": args.from_email,
        "template_id": args.template_id,
    }
    base = args.endpoint.rstrip("/")
    data = _post(
        f"{base}/api/campaigns",
        {"Authorization": f"token {args.token}", "Content-Type": "application/json"},
        payload,
    )
    cid = (data.get("data") or {}).get("id")
    return {
        "provider": "listmonk",
        "campaign_id": cid,
        "draft_url": f"{base}/admin/campaigns/{cid}" if cid else "",
        "status": "draft",
    }


def push_buttondown(html_body: str, args) -> dict:
    data = _post(
        "https://api.buttondown.email/v1/emails",
        {"Authorization": f"Token {args.token}", "Content-Type": "application/json"},
        {"subject": args.subject, "body": html_body, "status": "draft"},
    )
    cid = data.get("id")
    return {
        "provider": "buttondown",
        "campaign_id": cid,
        "draft_url": f"https://buttondown.com/emails/{cid}" if cid else "",
        "status": "draft",
    }


def push_kit(html_body: str, args) -> dict:
    """Kit (ConvertKit) v4: broadcasts are created as drafts by default."""
    data = _post(
        "https://api.kit.com/v4/broadcasts",
        {"Content-Type": "application/json", "X-Kit-Api-Key": args.token},
        {
            "subject": args.subject,
            "content": html_body,
            "description": args.name or args.subject,
            "public": False,
            "send_at": None,
        },
    )
    bid = (data.get("broadcast") or {}).get("id")
    return {
        "provider": "kit",
        "campaign_id": bid,
        "draft_url": f"https://app.kit.com/broadcasts/{bid}/edit" if bid else "",
        "status": "draft",
    }


PROVIDERS = {
    "mailerlite": push_mailerlite,
    "listmonk": push_listmonk,
    "buttondown": push_buttondown,
    "kit": push_kit,
}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _read(path: str) -> str:
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return fh.read()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("render", help="render a note into an HTML e-mail")
    r.add_argument("--file", required=True, help="Markdown/note file to render")
    r.add_argument("--out", help="Write HTML here (default: stdout)")
    r.add_argument("--subject", default="", help="Subject (used in <title>)")
    r.add_argument("--preheader", default="", help="Hidden preheader text")
    r.add_argument("--footer-note", default="You are receiving this e-mail because you subscribed.")

    s = sub.add_parser("push", help="render + create a DRAFT campaign")
    s.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    s.add_argument("--file", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--name", default="", help="Internal campaign name")
    s.add_argument("--from-name", default=os.environ.get("NEWSLETTER_FROM_NAME", ""))
    s.add_argument("--from-email", default=os.environ.get("NEWSLETTER_FROM_EMAIL", ""))
    s.add_argument("--token", default=os.environ.get("NEWSLETTER_TOKEN", ""))
    s.add_argument("--group", default="", help="MailerLite group ids (comma separated)")
    s.add_argument("--list-id", default="", help="Listmonk list ids (comma separated)")
    s.add_argument("--template-id", type=int, default=1, help="Listmonk template id")
    s.add_argument("--endpoint", default=os.environ.get("LISTMONK_URL", "http://127.0.0.1:9000"))
    s.add_argument("--preheader", default="")
    s.add_argument("--dry-run", action="store_true", help="Render + print JSON, no HTTP")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    md = _read(args.file)

    if args.command == "render":
        out = render_email(
            md,
            subject=args.subject,
            preheader=args.preheader,
            footer_note=args.footer_note,
        )
        if args.out:
            with open(os.path.expanduser(args.out), "w", encoding="utf-8") as fh:
                fh.write(out)
            print(f"Wrote {args.out} ({len(out)} bytes)")
        else:
            sys.stdout.write(out)
        return 0

    html_body = render_email(md, subject=args.subject, preheader=args.preheader)
    if not args.token and not args.dry_run:
        print("error: --token (or NEWSLETTER_TOKEN) is required", file=sys.stderr)
        return 2
    if args.dry_run:
        print(
            json.dumps(
                {
                    "provider": args.provider,
                    "subject": args.subject,
                    "bytes": len(html_body),
                    "preview": html_body[:220],
                },
                indent=2,
            )
        )
        return 0
    try:
        result = PROVIDERS[args.provider](html_body, args)
    except PushError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
