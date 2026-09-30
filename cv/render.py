#!/usr/bin/env python3
"""Render cv/README.md (the single source of truth) to cv/graehl-cv.pdf
and cv/graehl-cv.html.

The PDF uses the Awesome-CV LaTeX class (github.com/posquit0/Awesome-CV).
The HTML is a small self-contained page from the same parsed model; its
style and article sit between `<!-- cv:start -->` and `<!-- cv:end -->`
markers so another page can inline that fragment unchanged.
Requires `lualatex` from TeX Live with: fontawesome6, tcolorbox,
sourcesans, roboto, accsupp, and the LaTeX-recommended collection.
Awesome-CV is not on CTAN; its class file is downloaded at a pinned
commit and checked against a pinned hash.

The parser accepts only the forms cv/README.md uses and fails on
anything else, so a format change surfaces here rather than as a
silently dropped line.
"""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CV_DIR = Path(__file__).resolve().parent
SOURCE = CV_DIR / "README.md"
BUILD = CV_DIR / "build"
OUTPUT = CV_DIR / "graehl-cv.pdf"
HTML_OUTPUT = CV_DIR / "graehl-cv.html"

AWESOME_SHA = "6701180c71479588dae5d895c4a10a6a572a40a0"
AWESOME_CLS_SHA256 = "e961a0c6d7330cbfb8d2de31e1e6219d30f826df01965201b540f5695dc377ed"
AWESOME_URL = f"https://raw.githubusercontent.com/posquit0/Awesome-CV/{AWESOME_SHA}/awesome-cv.cls"

DASH = " — "
PDF_LINK = r"\[[^\]]+\]\(graehl-cv\.pdf\)"


class FormatError(Exception):
    pass


# ---------------------------------------------------------------- model


@dataclass
class Item:
    text: str
    details: list = field(default_factory=list)  # indented '  - ' bullets


@dataclass
class Group:
    title: str  # '### ' subheading; "" for items directly under '## '
    items: list = field(default_factory=list)


@dataclass
class Section:
    title: str
    paragraph: str = ""
    groups: list = field(default_factory=lambda: [Group("")])

    def items(self):
        return [i for g in self.groups for i in g.items]


@dataclass
class Cv:
    first: str
    last: str
    subtitle: str
    email: str
    github: str
    scholar: str  # Semantic Scholar author URL; "" when absent
    sections: list


def parse(text):
    blocks = [(n, l.rstrip()) for n, l in enumerate(text.splitlines(), 1) if l.strip()]
    if len(blocks) < 3:
        raise FormatError("expected name, subtitle, and contact lines")

    (n, name), (n2, sub), (n3, contact) = blocks[:3]
    m = re.fullmatch(r"# (.+)", name)
    if not m:
        raise FormatError(f"line {n}: expected '# <name>'")
    first, _, last = m.group(1).rpartition(" ")
    m = re.fullmatch(r"\*\*(.+)\*\*", sub)
    if not m:
        raise FormatError(f"line {n2}: expected '**<subtitle>**'")
    subtitle = m.group(1)
    email = re.search(r"\(mailto:([^)]+)\)", contact)
    github = re.search(r"\(https://github\.com/([^)/]+)\)", contact)
    if not (email and github):
        raise FormatError(f"line {n3}: expected mailto: and github.com links")
    scholar = re.search(
        r"\((https://www\.semanticscholar\.org/author/[^)]+)\)", contact
    )

    sections = []
    for n, l in blocks[3:]:
        if re.fullmatch(PDF_LINK, l):
            # The Markdown links to its own PDF rendition for web readers;
            # the PDF itself omits that line.
            continue
        if l.startswith("## "):
            sections.append(Section(l[3:].strip()))
            continue
        if not sections:
            raise FormatError(f"line {n}: content before first '## ' section")
        sec = sections[-1]
        if l.startswith("### "):
            if sec.groups[-1].title or sec.groups[-1].items:
                sec.groups.append(Group(l[4:].strip()))
            else:
                sec.groups[-1].title = l[4:].strip()
        elif l.startswith("- "):
            sec.groups[-1].items.append(Item(l[2:].strip()))
        elif l.startswith("  - "):
            if not sec.groups[-1].items:
                raise FormatError(f"line {n}: indented bullet without a parent item")
            if sec.title not in DATED:
                raise FormatError(f"line {n}: indented bullets only in {sorted(DATED)}")
            sec.groups[-1].items[-1].details.append(l[4:].strip())
        elif sec.items() or sec.paragraph:
            raise FormatError(f"line {n}: unexpected continuation line")
        else:
            sec.paragraph = l.strip()
    return Cv(
        first,
        last,
        subtitle,
        email.group(1),
        github.group(1),
        scholar.group(1) if scholar else "",
        sections,
    )


def split_dated(item, where):
    """'<date> — <a> — <b>' -> (date, a, b)."""
    parts = item.split(DASH)
    if len(parts) != 3:
        raise FormatError(f"{where}: expected '<date>{DASH}<a>{DASH}<b>': {item!r}")
    return tuple(p.strip() for p in parts)


def split_year(item, where):
    """'<text> (YYYY)<rest>' -> (year, text+rest)."""
    m = re.search(r"\s*\((\d{4})\)", item)
    if not m:
        raise FormatError(f"{where}: expected a '(YYYY)' year: {item!r}")
    return m.group(1), (item[: m.start()] + item[m.end() :]).strip()


# ---------------------------------------------------------------- inline

_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _escape(s):
    s = "".join(_SPECIAL.get(c, c) for c in s)
    # "quoted" -> ``quoted''
    return re.sub(r'"([^"]*)"', r"``\1''", s)


_INLINE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)|\*\*(.+?)\*\*|\*(.+?)\*")


def tex(md):
    """Markdown inline subset (links, bold, italic) -> LaTeX."""
    out, pos = [], 0
    for m in _INLINE.finditer(md):
        out.append(_escape(md[pos : m.start()]))
        if m.group(1) is not None:
            url = m.group(2).replace("%", r"\%").replace("#", r"\#")
            out.append(rf"\href{{{url}}}{{{tex(m.group(1))}}}")
        elif m.group(3) is not None:
            out.append(rf"\textbf{{{tex(m.group(3))}}}")
        else:
            out.append(rf"\emph{{{tex(m.group(4))}}}")
        pos = m.end()
    out.append(_escape(md[pos:]))
    return "".join(out)


# ---------------------------------------------------------------- sections
# Section kinds by title. Dated sections map '<date> — <a> — <b>' fields
# to (what, where): Education lists institution then degree; Experience
# lists role then organization.

DATED = {"Education": ("b", "a"), "Professional Experience": ("a", "b")}
HONORS = {"Honors"}


def dated_fields(sec, item):
    date, a, b = split_dated(item.text, sec.title)
    f = {"a": a, "b": b}
    what, where = DATED[sec.title]
    return date, f[what], f[where]


# ---------------------------------------------------------------- awesome-cv


def awesome_group(sec, group):
    if not group.items:
        return []
    o = [rf"\cvsubsection{{{tex(group.title)}}}", ""] if group.title else []
    if sec.title in DATED:
        o += [r"\begin{cventries}", ""]
        for item in group.items:
            date, what, where = dated_fields(sec, item)
            desc = ""
            if item.details:
                desc = "\n".join(
                    [r"\begin{cvitems}"]
                    + [rf"\item {{{tex(d)}}}" for d in item.details]
                    + [r"\end{cvitems}"]
                )
            o += [
                rf"\cventry{{{tex(what)}}}{{{tex(where)}}}{{}}{{{tex(date)}}}{{{desc}}}",
                "",
            ]
        o += [r"\end{cventries}", ""]
    elif sec.title in HONORS:
        o.append(r"\begin{cvhonors}")
        for item in group.items:
            year, text = split_year(item.text, sec.title)
            o.append(rf"\cvhonor{{{tex(text)}}}{{}}{{}}{{{year}}}")
        o += [r"\end{cvhonors}", ""]
    else:
        o += [r"\vspace{2mm}", r"\begin{cvitems}"]
        o += [rf"\item {{{tex(i.text)}}}" for i in group.items]
        o += [r"\end{cvitems}", r"\vspace{2mm}", ""]
    return o


def awesome(cv):
    o = [
        r"\documentclass[11pt, letterpaper]{awesome-cv}",
        r"\geometry{left=1.6cm, top=1.2cm, right=1.6cm, bottom=1.6cm, footskip=.5cm}",
        r"\colorlet{awesome}{awesome-skyblue}",
        r"\setbool{acvSectionColorHighlight}{true}",
        rf"\name{{{tex(cv.first)}}}{{{tex(cv.last)}}}",
        rf"\position{{{tex(cv.subtitle)}}}",
        rf"\email{{{cv.email}}}",
        rf"\github{{{cv.github}}}",
    ]
    if cv.scholar:
        o.append(
            rf"\extrainfo{{\href{{{cv.scholar}}}{{\faGraduationCap\ Semantic Scholar}}}}"
        )
    o += [
        r"\begin{document}",
        r"\makecvheader",
        rf"\makecvfooter{{}}{{{tex(cv.first)} {tex(cv.last)}~~~\textperiodcentered~~~{tex(cv.subtitle)}}}{{\thepage}}",
        "",
    ]
    # Blank lines (paragraph breaks) after sections and between entries
    # follow the upstream examples; cvhonors is a tabular and must not
    # contain them.
    for sec in cv.sections:
        o += [rf"\cvsection{{{tex(sec.title)}}}", ""]
        if sec.paragraph:
            o += [r"\begin{cvparagraph}", tex(sec.paragraph), r"\end{cvparagraph}", ""]
        for group in sec.groups:
            o += awesome_group(sec, group)
    o.append(r"\end{document}")
    return "\n".join(o) + "\n"


# ---------------------------------------------------------------- html

_HTML_CSS = """\
.cv{--ink:#192c3d;--blue:#0395de;--muted:#526477;--rule:#d5e1e9;color:var(--ink);font:11pt/1.42 "Source Sans 3","Source Sans Pro","Segoe UI",Helvetica,Arial,sans-serif}
.cv h1,.cv h2,.cv h3{font-family:Roboto,"Segoe UI",Helvetica,Arial,sans-serif;font-weight:700}
.cv h1{font-size:24pt;line-height:1.1;letter-spacing:-.025em;margin:0}
.cv .subtitle{font-size:12pt;color:var(--muted);margin:2px 0 4px}
.cv .contact{margin:0 0 6px}.cv .contact a+a::before{content:" · ";color:var(--muted)}
.cv h2{font-size:13pt;color:#126a98;border-bottom:2px solid var(--blue);padding-bottom:3px;margin:20px 0 8px;text-transform:uppercase;letter-spacing:.05em}
.cv h3{font-size:11pt;margin:12px 0 5px}
.cv p{margin:0 0 8px}.cv ul{margin:0 0 6px;padding-left:1.25em}.cv li{margin:0 0 4px}
.cv .entries{list-style:none;padding:0}.cv .entries>li{display:grid;grid-template-columns:7.5em 1fr;gap:0 12px;margin:0 0 8px}
.cv .date{color:var(--muted);font-variant-numeric:tabular-nums}.cv .where{color:var(--muted)}
.cv .entries ul{margin:3px 0 0;padding-left:1.1em}.cv .entries ul li{margin:0 0 2px}
.cv .pdf{margin-top:20px;font-size:9.5pt;color:var(--muted)}
.cv a{color:#126a98;text-decoration:none}.cv a:hover{text-decoration:underline}
@media (max-width:600px){.cv .entries>li{grid-template-columns:1fr}.cv .date{font-size:9.5pt}}
"""


def _html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def html_inline(md):
    """Markdown inline subset (links, bold, italic) -> HTML."""
    out, pos = [], 0
    for m in _INLINE.finditer(md):
        out.append(_html_escape(md[pos : m.start()]))
        if m.group(1) is not None:
            url = m.group(2).replace('"', "%22")
            out.append(f'<a href="{url}">{html_inline(m.group(1))}</a>')
        elif m.group(3) is not None:
            out.append(f"<strong>{html_inline(m.group(3))}</strong>")
        else:
            out.append(f"<em>{html_inline(m.group(4))}</em>")
        pos = m.end()
    out.append(_html_escape(md[pos:]))
    return "".join(out)


def html_group(sec, group):
    if not group.items:
        return []
    o = [f"<h3>{html_inline(group.title)}</h3>"] if group.title else []
    if sec.title in DATED:
        o.append('<ul class="entries">')
        for item in group.items:
            date, what, where = dated_fields(sec, item)
            o.append(
                f'<li><span class="date">{html_inline(date)}</span><div><strong>{html_inline(what)}</strong>'
                f' <span class="where">· {html_inline(where)}</span>'
            )
            if item.details:
                o.append("<ul>" + "".join(f"<li>{html_inline(d)}</li>" for d in item.details) + "</ul>")
            o.append("</div></li>")
        o.append("</ul>")
    elif sec.title in HONORS:
        o.append('<ul class="entries">')
        for item in group.items:
            year, text = split_year(item.text, sec.title)
            o.append(f'<li><span class="date">{year}</span><div>{html_inline(text)}</div></li>')
        o.append("</ul>")
    else:
        o.append("<ul>" + "".join(f"<li>{html_inline(i.text)}</li>" for i in group.items) + "</ul>")
    return o


def html(cv):
    name = f"{html_inline(cv.first)} {html_inline(cv.last)}"
    contact = [
        f'<a href="mailto:{cv.email}">{cv.email}</a>',
        f'<a href="https://github.com/{cv.github}">github.com/{cv.github}</a>',
    ]
    if cv.scholar:
        contact.append(f'<a href="{cv.scholar}">Semantic Scholar</a>')
    o = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{name} — {html_inline(cv.subtitle)}</title>",
        "<style>body{margin:0;background:#edf2f5}main{max-width:8.5in;margin:24px auto;padding:.6in .7in;background:#fff}"
        "@media (max-width:600px){main{margin:0;padding:20px}}</style>",
        "</head><body><main>",
        "<!-- cv:start -->",
        f"<style>{_HTML_CSS}</style>",
        '<article class="cv">',
        f"<header><h1>{name}</h1><p class=\"subtitle\">{html_inline(cv.subtitle)}</p>"
        f'<p class="contact">{"".join(contact)}</p></header>',
    ]
    for sec in cv.sections:
        o.append(f"<section><h2>{html_inline(sec.title)}</h2>")
        if sec.paragraph:
            o.append(f"<p>{html_inline(sec.paragraph)}</p>")
        for group in sec.groups:
            o += html_group(sec, group)
        o.append("</section>")
    o += [
        f'<p class="pdf"><a href="{OUTPUT.name}">PDF version of this CV</a></p>',
        "</article>",
        "<!-- cv:end -->",
        "</main></body></html>",
    ]
    return "\n".join(o) + "\n"


# ---------------------------------------------------------------- build


def fetch_awesome_cls(dest):
    if (
        dest.exists()
        and hashlib.sha256(dest.read_bytes()).hexdigest() == AWESOME_CLS_SHA256
    ):
        return
    with urllib.request.urlopen(AWESOME_URL) as r:
        data = r.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != AWESOME_CLS_SHA256:
        raise SystemExit(f"awesome-cv.cls hash mismatch: got {digest}")
    dest.write_bytes(data)


def build(cv):
    work = BUILD
    work.mkdir(parents=True, exist_ok=True)
    fetch_awesome_cls(work / "awesome-cv.cls")
    (work / "cv.tex").write_text(awesome(cv))
    log = work / "lualatex.log"
    with log.open("w") as f:
        # Two passes settle hyperref/bookmark references.
        for _ in range(2):
            r = subprocess.run(
                ["lualatex", "-interaction=nonstopmode", "-halt-on-error", "cv.tex"],
                cwd=work,
                stdout=f,
                stderr=subprocess.STDOUT,
            )
            if r.returncode:
                raise SystemExit(f"lualatex failed; see {log}")
    shutil.copyfile(work / "cv.pdf", OUTPUT)
    print(OUTPUT.relative_to(CV_DIR.parent))
    HTML_OUTPUT.write_text(html(cv))
    print(HTML_OUTPUT.relative_to(CV_DIR.parent))


def main():
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args()
    if not shutil.which("lualatex"):
        raise SystemExit("lualatex not found on PATH (install TeX Live)")
    try:
        build(parse(SOURCE.read_text()))
    except FormatError as e:
        raise SystemExit(f"{SOURCE.name}: {e}")


if __name__ == "__main__":
    sys.exit(main())
