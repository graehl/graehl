#!/usr/bin/env python3
"""Render cv/README.md (the single source of truth) to PDF.

Produces one PDF per LaTeX CV class:
  cv/graehl-cv-awesome.pdf   Awesome-CV (github.com/posquit0/Awesome-CV)
  cv/graehl-cv-moderncv.pdf  moderncv (CTAN)

Requires `lualatex` from TeX Live with: moderncv, fontawesome5,
fontawesome6, academicons, tcolorbox, sourcesans, roboto, and the
LaTeX-recommended collection. The Awesome-CV class is not on CTAN; it is
downloaded at a pinned commit and checked against a pinned hash.

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

AWESOME_SHA = "6701180c71479588dae5d895c4a10a6a572a40a0"
AWESOME_CLS_SHA256 = "e961a0c6d7330cbfb8d2de31e1e6219d30f826df01965201b540f5695dc377ed"
AWESOME_URL = f"https://raw.githubusercontent.com/posquit0/Awesome-CV/{AWESOME_SHA}/awesome-cv.cls"

DASH = " — "


class FormatError(Exception):
    pass


# ---------------------------------------------------------------- model


@dataclass
class Section:
    title: str
    paragraph: str = ""
    items: list = field(default_factory=list)


@dataclass
class Cv:
    first: str
    last: str
    subtitle: str
    email: str
    github: str
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

    sections = []
    for n, l in blocks[3:]:
        if l.startswith("## "):
            sections.append(Section(l[3:].strip()))
        elif not sections:
            raise FormatError(f"line {n}: content before first '## ' section")
        elif l.startswith("- "):
            sections[-1].items.append(l[2:].strip())
        elif sections[-1].items or sections[-1].paragraph:
            raise FormatError(f"line {n}: unexpected continuation line")
        else:
            sections[-1].paragraph = l.strip()
    return Cv(first, last, subtitle, email.group(1), github.group(1), sections)


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
    date, a, b = split_dated(item, sec.title)
    f = {"a": a, "b": b}
    what, where = DATED[sec.title]
    return date, f[what], f[where]


# ---------------------------------------------------------------- awesome-cv


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
        if not sec.items:
            continue
        if sec.title in DATED:
            o += [r"\begin{cventries}", ""]
            for item in sec.items:
                date, what, where = dated_fields(sec, item)
                o += [
                    rf"\cventry{{{tex(what)}}}{{{tex(where)}}}{{}}{{{tex(date)}}}{{}}",
                    "",
                ]
            o += [r"\end{cventries}", ""]
        elif sec.title in HONORS:
            o.append(r"\begin{cvhonors}")
            for item in sec.items:
                year, text = split_year(item, sec.title)
                o.append(rf"\cvhonor{{{tex(text)}}}{{}}{{}}{{{year}}}")
            o += [r"\end{cvhonors}", ""]
        else:
            o += [r"\vspace{2mm}", r"\begin{cvitems}"]
            o += [rf"\item {{{tex(i)}}}" for i in sec.items]
            o += [r"\end{cvitems}", r"\vspace{2mm}", ""]
    o.append(r"\end{document}")
    return "\n".join(o) + "\n"


# ---------------------------------------------------------------- moderncv


def moderncv(cv):
    o = [
        r"\documentclass[11pt, letterpaper, sans]{moderncv}",
        # Color before style: the style copies color1 into its section/rule
        # colors when loaded (moderncv 2.6).
        r"\moderncvcolor{blue}",
        r"\moderncvstyle{classic}",
        r"\usepackage[scale=0.8]{geometry}",
        rf"\name{{{tex(cv.first)}}}{{{tex(cv.last)}}}",
        rf"\title{{{tex(cv.subtitle)}}}",
        rf"\email{{{cv.email}}}",
        rf"\social[github]{{{cv.github}}}",
        r"\begin{document}",
        r"\makecvtitle",
    ]
    for sec in cv.sections:
        o.append(rf"\section{{{tex(sec.title)}}}")
        if sec.paragraph:
            o.append(rf"\cvitem{{}}{{{tex(sec.paragraph)}}}")
        for item in sec.items:
            if sec.title in DATED:
                date, what, where = dated_fields(sec, item)
                o.append(
                    rf"\cventry{{{tex(date)}}}{{{tex(what)}}}{{{tex(where)}}}{{}}{{}}{{}}"
                )
            elif sec.title in HONORS:
                year, text = split_year(item, sec.title)
                o.append(rf"\cvitem{{{year}}}{{{tex(text)}}}")
            else:
                o.append(rf"\cvlistitem{{{tex(item)}}}")
    o.append(r"\end{document}")
    return "\n".join(o) + "\n"


# ---------------------------------------------------------------- build

STYLES = {"awesome": awesome, "moderncv": moderncv}


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


def build(style, cv):
    work = BUILD / style
    work.mkdir(parents=True, exist_ok=True)
    if style == "awesome":
        fetch_awesome_cls(work / "awesome-cv.cls")
    (work / "cv.tex").write_text(STYLES[style](cv))
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
                raise SystemExit(f"{style}: lualatex failed; see {log}")
    out = CV_DIR / f"graehl-cv-{style}.pdf"
    shutil.copyfile(work / "cv.pdf", out)
    print(out.relative_to(CV_DIR.parent))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "styles",
        nargs="*",
        metavar="style",
        help=f"CV classes to render: {', '.join(STYLES)} (default: all)",
    )
    args = ap.parse_args()
    unknown = set(args.styles) - set(STYLES)
    if unknown:
        ap.error(f"unknown style(s): {', '.join(sorted(unknown))}")
    args.styles = args.styles or list(STYLES)
    if not shutil.which("lualatex"):
        raise SystemExit("lualatex not found on PATH (install TeX Live)")
    try:
        cv = parse(SOURCE.read_text())
        for style in args.styles:
            build(style, cv)
    except FormatError as e:
        raise SystemExit(f"{SOURCE.name}: {e}")


if __name__ == "__main__":
    sys.exit(main())
