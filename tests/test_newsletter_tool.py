"""Unit tests for scripts/newsletter_tool.py (Markdown -> HTML e-mail)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import newsletter_tool as nt  # noqa: E402


def test_frontmatter_is_stripped():
    md = "---\ntype: note\nstatus: draft\n---\n\nHello world\n"
    assert "type: note" not in nt.strip_frontmatter(md)
    html = nt.md_to_html(nt.strip_frontmatter(md))
    assert "<p" in html and "Hello world" in html


def test_wikilinks_and_embeds():
    md = "See [[TODO/Other Note|the other note]] and ![[pic.png|A cat]]"
    out = nt.obsidian_to_markdown(md)
    assert "](TODO/Other%20Note)" in out and "the other note" in out
    assert "![A cat](pic.png)" in out


def test_comments_removed_and_highlight_bold():
    out = nt.obsidian_to_markdown("keep %%drop me%% this ==loud==")
    assert "drop me" not in out
    assert "**loud**" in out


def test_headings_lists_and_tables():
    md = """
# Title

- one
- two

1. first
2. second

| A | B |
| --- | --- |
| 1 | 2 |
"""
    html = nt.md_to_html(md)
    assert "<h1" in html
    assert html.count("<ul") == 1 and html.count("<ol") == 1
    assert "<table" in html and "<th" in html and "<td" in html


def test_inline_styles_and_inline_code_protected():
    html = nt.md_to_html("Run `a|b` then **bold** and _em_ and <script>x</script>")
    assert "<code" in html and "a|b" in html
    assert "<strong>bold</strong>" in html and "<em>em</em>" in html
    assert "&lt;script&gt;" in html  # escaped, no raw HTML injection


def test_callout_becomes_blockquote():
    md = "> [!tip] Read this\n> body line"
    html = nt.md_to_html(md)
    assert "<blockquote" in html and "Read this" in html and "body line" in html


def test_fenced_code_is_escaped():
    html = nt.md_to_html("```\n<b>raw</b>\n```")
    assert "&lt;b&gt;raw&lt;/b&gt;" in html


def test_render_email_wraps_with_unsubscribe():
    html = nt.render_email("# Hi\n\nbody", subject="Subj", preheader="Pre")
    assert html.startswith("<!DOCTYPE html>")
    assert "<title>Subj</title>" in html
    assert "Unsubscribe" in html and "Pre" in html


def test_cli_render_to_file(tmp_path):
    note = tmp_path / "n.md"
    note.write_text("---\ntitle: x\n---\n\n# Hello\n", encoding="utf-8")
    out = tmp_path / "out.html"
    assert nt.main(["render", "--file", str(note), "--out", str(out), "--subject", "S"]) == 0
    assert "<h1" in out.read_text(encoding="utf-8")


def test_cli_push_dry_run_never_uses_network(tmp_path, capsys):
    note = tmp_path / "n.md"
    note.write_text("# Hello\n", encoding="utf-8")
    rc = nt.main(
        ["push", "--provider", "mailerlite", "--file", str(note), "--subject", "S", "--dry-run"]
    )
    assert rc == 0
    assert '"provider": "mailerlite"' in capsys.readouterr().out
