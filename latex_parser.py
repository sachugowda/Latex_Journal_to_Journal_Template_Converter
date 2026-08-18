"""
latex_parser.py
----------------
Parses an arbitrary journal-formatted LaTeX paper (IEEE, Springer, Elsevier,
ACM, plain article, etc.) into a template-agnostic intermediate
representation (IR), stored as JSON.

This is intentionally regex/heuristic based rather than a full LaTeX AST
parser (writing a complete TeX parser is its own multi-year project), but it
is built to be forgiving: if something doesn't match a known pattern, it is
preserved verbatim in the IR under "unparsed_preamble" or inside the nearest
section body, and flagged in "review_notes" rather than silently dropped.

Usage:
    python3 latex_parser.py input.tex -o paper.ir.json
"""

import re
import json
import argparse
import sys
from pathlib import Path


SECTION_CMDS = ["section", "section\\*", "subsection", "subsection\\*",
                "subsubsection", "subsubsection\\*"]

SECTION_LEVEL = {
    "section": 1, "section*": 1,
    "subsection": 2, "subsection*": 2,
    "subsubsection": 3, "subsubsection*": 3,
}


def resolve_input_includes(text, base_dir, _depth=0, _seen=None):
    """
    Recursively replace \\input{file} and \\include{file} with the contents
    of that file (searched relative to base_dir, trying both 'name' and
    'name.tex'). Guards against cycles and runaway depth. Files that can't
    be found are left as a commented marker so nothing silently vanishes.
    """
    if _seen is None:
        _seen = set()
    if _depth > 15:
        return text

    pattern = re.compile(r'\\(input|include)\{([^}]+)\}')

    def replace(m):
        kind, name = m.group(1), m.group(2).strip()
        for candidate in [name, name + ".tex"]:
            p = Path(base_dir) / candidate
            if p.exists():
                key = str(p.resolve())
                if key in _seen:
                    return f"% [skipped \\{kind}{{{name}}} — circular reference]"
                new_seen = _seen | {key}
                sub = p.read_text(encoding="utf-8", errors="ignore")
                sub = strip_comments(sub)
                return resolve_input_includes(sub, base_dir, _depth + 1, new_seen)
        return f"% [could not resolve \\{kind}{{{name}}} — file not found in project]"

    return pattern.sub(replace, text)


CUSTOM_MACRO_CMDS = ["newcommand", "renewcommand", "newenvironment",
                     "DeclareMathOperator", "def"]


def extract_custom_macros(preamble):
    """
    Best-effort extraction of user-defined macros from the preamble, so they
    can be carried over into the converted document (they are NOT part of
    any journal's document class and are almost always still needed).
    Returns a list of raw macro-definition lines/blocks.
    """
    macros = []
    for cmd in CUSTOM_MACRO_CMDS:
        search_from = 0
        while True:
            idx = preamble.find("\\" + cmd + "{", search_from)
            if idx == -1:
                break
            # capture the whole statement: \cmd{name}[nargs]{body} or \cmd{name}{body}
            # walk forward collecting balanced-brace groups until a non-brace,
            # non-bracket character follows (end of statement)
            i = idx
            end = i + len(cmd) + 1
            j = end
            while j < len(preamble) and preamble[j] in "{[":
                if preamble[j] == '{':
                    depth = 1
                    j += 1
                    while j < len(preamble) and depth > 0:
                        if preamble[j] == '{':
                            depth += 1
                        elif preamble[j] == '}':
                            depth -= 1
                        j += 1
                elif preamble[j] == '[':
                    depth = 1
                    j += 1
                    while j < len(preamble) and depth > 0:
                        if preamble[j] == '[':
                            depth += 1
                        elif preamble[j] == ']':
                            depth -= 1
                        j += 1
            macros.append(preamble[idx:j].strip())
            search_from = j
    return macros


def remove_balanced_command(text, command):
    """Remove ALL occurrences of \\command{...} (balanced braces) from text."""
    out = []
    i = 0
    marker = "\\" + command
    while True:
        idx = text.find(marker, i)
        if idx == -1:
            out.append(text[i:])
            break
        # ensure this isn't a prefix of a longer command name
        end_of_name = idx + len(marker)
        if end_of_name < len(text) and text[end_of_name].isalpha():
            out.append(text[i:idx + len(marker)])
            i = idx + len(marker)
            continue
        out.append(text[i:idx])
        j = end_of_name
        # skip an optional trailing '*' (e.g. \section*, \subsection*)
        if j < len(text) and text[j] == '*':
            j += 1
        # skip an optional [..] group (e.g. \author*[1,2]{...})
        while j < len(text) and text[j] in " \t\n":
            j += 1
        if j < len(text) and text[j] == '[':
            depth = 1
            j += 1
            while j < len(text) and depth > 0:
                if text[j] == '[':
                    depth += 1
                elif text[j] == ']':
                    depth -= 1
                j += 1
        while j < len(text) and text[j] in " \t\n":
            j += 1
        if j < len(text) and text[j] == '{':
            depth = 1
            j += 1
            while j < len(text) and depth > 0:
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                j += 1
            i = j
        else:
            i = end_of_name
    return "".join(out)


def find_main_tex(project_dir):
    """
    Heuristic main-file detection within a project directory: a .tex file
    containing both \\documentclass and \\begin{document}. If several
    qualify, prefer one named main.tex/paper.tex/manuscript.tex/sample.tex
    (common template names); otherwise take the largest.
    Returns (path_or_None, list_of_all_candidates).
    """
    candidates = []
    for p in Path(project_dir).rglob("*.tex"):
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "\\documentclass" in content and "\\begin{document}" in content:
            candidates.append(p)

    if not candidates:
        return None, []

    preferred_names = {"main.tex", "paper.tex", "manuscript.tex", "article.tex", "sample.tex"}
    for p in candidates:
        if p.name.lower() in preferred_names:
            return p, candidates

    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0], candidates


def strip_comments(text):
    """Remove LaTeX comments (unescaped %) line by line."""
    out_lines = []
    for line in text.split("\n"):
        # don't strip \%
        line = re.sub(r'(?<!\\)%.*$', '', line)
        out_lines.append(line)
    return "\n".join(out_lines)


def extract_env(text, env_name):
    """Return (content, span) of the first \\begin{env}...\\end{env}, or (None, None)."""
    pattern = re.compile(
        r'\\begin\{' + re.escape(env_name) + r'\*?\}(.*?)\\end\{' + re.escape(env_name) + r'\*?\}',
        re.DOTALL
    )
    m = pattern.search(text)
    if not m:
        return None, None
    return m.group(1).strip(), m.span()


def extract_command_arg(text, command):
    """Extract the brace-balanced argument of \\command{...}. Returns first match or None.
    Also matches \\command*{...} (the optional star used by \\section*, \\cite*, etc.)."""
    idx = text.find("\\" + command)
    if idx == -1:
        return None
    i = idx + len(command) + 1
    # skip an optional trailing '*' (e.g. \section*, \subsection*)
    if i < len(text) and text[i] == '*':
        i += 1
    # skip whitespace/optional args [..]
    while i < len(text) and text[i] in " \t\n":
        i += 1
    while i < len(text) and text[i] == '[':
        depth = 1
        i += 1
        while i < len(text) and depth > 0:
            if text[i] == '[':
                depth += 1
            elif text[i] == ']':
                depth -= 1
            i += 1
        while i < len(text) and text[i] in " \t\n":
            i += 1
    if i >= len(text) or text[i] != '{':
        return None
    depth = 1
    start = i + 1
    i += 1
    while i < len(text) and depth > 0:
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
        i += 1
    return text[start:i-1].strip()


def find_all_commands(text, command):
    """Find all occurrences of \\command{...}, return list of arg strings."""
    results = []
    search_from = 0
    while True:
        idx = text.find("\\" + command, search_from)
        if idx == -1:
            break
        # ensure not a prefix of a longer command name
        end_of_name = idx + 1 + len(command)
        if end_of_name < len(text) and (text[end_of_name].isalpha()):
            search_from = idx + 1
            continue
        arg = extract_command_arg(text[idx:], command)
        if arg is not None:
            results.append(arg)
        search_from = idx + len(command) + 1
    return results


def split_sections(body):
    """
    Split the document body into a flat list of section dicts:
    {level, title, content}
    based on \\section, \\subsection, \\subsubsection commands.
    Content before the first section heading is captured as level 0 ("preamble body").
    """
    # Build a regex that finds section command positions
    cmd_pattern = re.compile(
        r'\\(section\*?|subsection\*?|subsubsection\*?)\{'
    )
    matches = []
    for m in cmd_pattern.finditer(body):
        cmd = m.group(1)
        # extract the balanced-brace title starting at m.end()-1 (the '{')
        title = extract_command_arg(body[m.start():], cmd.rstrip('*'))
        matches.append({
            "start": m.start(),
            "cmd": cmd,
            "title": title if title else "(untitled)",
        })

    sections = []
    if not matches:
        return [{"level": 0, "title": None, "content": body.strip()}]

    if matches[0]["start"] > 0:
        pre = body[:matches[0]["start"]].strip()
        if pre:
            sections.append({"level": 0, "title": None, "content": pre})

    for i, m in enumerate(matches):
        # find end of the title command to know where content starts
        after_title_idx = body.find("}", m["start"])
        # content spans from just after this heading's full command to the next heading's start
        # find actual end of the \sectionname{...} command (balanced)
        title_arg_start = body.find("{", m["start"])
        depth = 1
        j = title_arg_start + 1
        while j < len(body) and depth > 0:
            if body[j] == '{':
                depth += 1
            elif body[j] == '}':
                depth -= 1
            j += 1
        content_start = j
        content_end = matches[i+1]["start"] if i + 1 < len(matches) else len(body)
        content = body[content_start:content_end].strip()
        sections.append({
            "level": SECTION_LEVEL.get(m["cmd"], 1),
            "title": m["title"],
            "content": content,
        })

    return sections


def extract_figures(text):
    figures = []
    for m in re.finditer(r'\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}', text, re.DOTALL):
        block = m.group(1)
        caption = extract_command_arg(block, "caption")
        label = extract_command_arg(block, "label")
        includegraphics = find_all_commands(block, "includegraphics")
        figures.append({
            "caption": caption,
            "label": label,
            "includegraphics": includegraphics,
            "raw": block.strip(),
        })
    return figures


def extract_tables(text):
    tables = []
    for m in re.finditer(r'\\begin\{table\*?\}(.*?)\\end\{table\*?\}', text, re.DOTALL):
        block = m.group(1)
        caption = extract_command_arg(block, "caption")
        label = extract_command_arg(block, "label")
        tables.append({
            "caption": caption,
            "label": label,
            "raw": block.strip(),
        })
    return tables


def extract_bibliography(text, source_dir):
    """
    Detect either:
      - \\bibliography{name} pointing to a .bib file (returns path + raw bib text if found)
      - an inline thebibliography environment
    """
    result = {"bib_files": [], "bib_content": None, "inline_bibliography": None,
              "bibliographystyle": None}

    style = extract_command_arg(text, "bibliographystyle")
    result["bibliographystyle"] = style

    bib_names = find_all_commands(text, "bibliography")
    if bib_names:
        names = [n.strip() for n in bib_names[0].split(",")]
        result["bib_files"] = names
        combined = []
        for n in names:
            for candidate in [n, n + ".bib"]:
                p = Path(source_dir) / candidate
                if p.exists():
                    combined.append(p.read_text(encoding="utf-8", errors="ignore"))
                    break
        if combined:
            result["bib_content"] = "\n\n".join(combined)

    inline, _ = extract_env(text, "thebibliography")
    if inline:
        result["inline_bibliography"] = inline.strip()

    return result


def strip_email_footnote(text):
    """Remove a trailing '(e-mail: ...).' parenthetical from a \\thanks{}
    sentence, and the leading 'X, Y, and Z are/is with the ' name-list
    prefix, leaving just the affiliation description."""
    # drop the (e-mail: ...) parenthetical, with or without a trailing period
    text = re.sub(r'\(\s*e-?mail\s*:.*?\)\.?', '', text, flags=re.IGNORECASE | re.DOTALL)
    # drop the leading "<names> are/is with the " prefix
    m = re.search(r'\b(?:is|are)\s+with\s+(?:the\s+)?(.*)', text, re.IGNORECASE | re.DOTALL)
    if m:
        text = m.group(1)
    return text.strip().rstrip(".").strip()


def extract_emails_from_thanks(thanks_text):
    """
    IEEE \\thanks{} blocks often give a single shared email domain for a
    group of authors via braced shorthand, e.g.
    '(e-mail: \\{sachin.d.nagaraju, ashkan.moradi\\}@ntnu.no)' — meaning
    each listed local-part shares that domain. Returns a list of full
    email addresses in the order they appear (empty list if none found).
    Also handles a single plain 'name@domain' email.
    """
    m = re.search(r'\\\{([^}]*)\\\}@([\w.\-]+)', thanks_text)
    if m:
        domain = m.group(2)
        local_parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
        return [f"{lp}@{domain}" for lp in local_parts]
    m = re.search(r'([\w.+\-]+@[\w.\-]+)', thanks_text)
    if m:
        return [m.group(1)]
    return []


def parse_ieee_thanks_style(block):
    """
    Handles IEEE's common per-group \\thanks{} affiliation convention,
    where \\author{} contains a plain-English, comma/'and'-separated name
    list (NOT \\IEEEauthorblockN and not LaTeX \\and) followed by one or
    more \\thanks{...} blocks, each describing the affiliation (and often
    a shared/braced email) for a subset of the named authors, e.g.:

        \\author{A, B, and C
          \\thanks{A, B, and C are with Dept X, Univ Y
                   (e-mail: \\{a, b, c\\}@example.edu).}
        }

    Returns a list of {"name", "affiliation", "email"} dicts, or None if
    this block doesn't look like this style (caller should fall back to
    other parsing strategies).
    """
    thanks_texts = find_all_commands(block, "thanks")
    if not thanks_texts:
        return None

    names_only = remove_balanced_command(block, "thanks")
    names_only = names_only.replace("\\\\", " ")
    # Split "A, B, and C" / "A, B and C" into individual names — this is
    # the plain-English list format, distinct from LaTeX's \and separator.
    raw_parts = re.split(r'\s*,\s*(?:and\s+)?|\s+and\s+', names_only.strip())
    names = [re.sub(r'\\\w+(\{.*?\})?', '', p).strip() for p in raw_parts]
    names = [n for n in names if n]
    if not names:
        return None

    authors = []
    for name in names:
        affiliation, email = None, None
        for thanks_text in thanks_texts:
            if name.lower() not in thanks_text.lower():
                continue
            affiliation = strip_email_footnote(thanks_text)
            emails = extract_emails_from_thanks(thanks_text)
            if len(emails) == 1:
                email = emails[0]
            elif len(emails) > 1:
                # Multiple emails shared across a \thanks block covering
                # multiple names — match by position among the names that
                # this same \thanks block mentions, in the order they're
                # named, if the counts line up.
                names_in_block = [n for n in names if n.lower() in thanks_text.lower()]
                if len(names_in_block) == len(emails) and name in names_in_block:
                    email = emails[names_in_block.index(name)]
            break
        # A single \thanks sentence sometimes covers TWO affiliations for
        # one person ("X is with A, and also with B") — split those into
        # separate entries so each institution can be numbered on its own,
        # rather than flattened into one run-on affiliation string.
        affiliations = None
        if affiliation:
            affiliations = [a.strip() for a in
                             re.split(r',?\s*and also with(?:\s+the)?\s+', affiliation,
                                       flags=re.IGNORECASE) if a.strip()]
            affiliation = affiliations[0]
        authors.append({"name": name, "affiliation": affiliation,
                         "affiliations": affiliations, "email": email})
    return authors


def find_numbered_blocks(text, command):
    """
    Finds all \\command*[N,M,...]{...} occurrences (Springer Nature's
    \\author*[1,2]{...} / \\affil[1]{...} convention) — a numbered-bracket
    variant that find_all_commands doesn't handle. Returns a list of dicts:
    {"starred": bool, "nums": [int,...], "content": str, "end": int}
    (end = index right after the closing brace, for scanning what follows).
    """
    results = []
    pattern = re.compile(r'\\' + re.escape(command) + r'(\*)?\[([\d,\s]+)\]')
    for m in pattern.finditer(text):
        starred = bool(m.group(1))
        nums = [int(n.strip()) for n in m.group(2).split(",") if n.strip()]
        i = m.end()
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i >= len(text) or text[i] != '{':
            continue
        depth = 1
        j = i + 1
        while j < len(text) and depth > 0:
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
            j += 1
        results.append({"starred": starred, "nums": nums,
                         "content": text[i+1:j-1], "end": j})
    return results


def parse_sn_style_authors(text):
    """
    Springer Nature (sn-jnl.cls) style: each author gets their OWN separate
    \\author*[N]{\\fnm{}\\sur{}} command (not one combined \\author{A \\and B}
    block like other classes), with a matching numbered \\affil[N]{...} list
    using \\orgdiv{}/\\orgname{}/\\orgaddress{} sub-fields, and often a
    trailing \\email{} right after each author entry. Returns a list of
    {"name", "affiliation", "affiliations", "email"} dicts, or None if this
    text doesn't look like this style (caller should try other strategies).
    """
    author_blocks = find_numbered_blocks(text, "author")
    if not author_blocks:
        return None

    affil_blocks = find_numbered_blocks(text, "affil")
    affil_map = {}
    for ab in affil_blocks:
        orgdiv = extract_command_arg(ab["content"], "orgdiv")
        orgname = extract_command_arg(ab["content"], "orgname")
        orgaddress_arg = extract_command_arg(ab["content"], "orgaddress")
        addr_parts = []
        if orgaddress_arg:
            for sub in ["street", "city", "postcode", "state", "country"]:
                v = extract_command_arg(orgaddress_arg, sub)
                if v:
                    addr_parts.append(v)
        combined = ", ".join(p for p in [orgdiv, orgname, *addr_parts] if p)
        for n in ab["nums"]:
            affil_map[n] = combined or None

    authors = []
    for i, ablock in enumerate(author_blocks):
        fnm = extract_command_arg(ablock["content"], "fnm")
        sur = extract_command_arg(ablock["content"], "sur")
        if not (fnm or sur):
            continue  # this \author[N]{} match isn't actually fnm/sur-style content
        name = " ".join(p for p in [fnm, sur] if p)
        next_start = author_blocks[i + 1]["end"] if i + 1 < len(author_blocks) else len(text)
        email = extract_command_arg(text[ablock["end"]:next_start], "email")
        affs = [affil_map[n] for n in ablock["nums"] if affil_map.get(n)]
        authors.append({
            "name": name,
            "affiliation": affs[0] if affs else None,
            "affiliations": affs if len(affs) > 1 else None,
            "email": email,
        })
    return authors if authors else None


def parse_authors_generic(preamble):
    """
    Best-effort author/affiliation extraction across common styles:
    - \\author{A \\and B}  (plain article / many springer classes)
    - \\author{...\\IEEEauthorblockN{}\\IEEEauthorblockA{}...}  (IEEE)
    - \\author{A}\\affil{X}  or  \\author{A}{X}  (varies)
    Returns list of {"name":..., "affiliation": ...} plus raw fallback.
    """
    # Springer Nature style uses separate \author*[N]{\fnm{}\sur{}} commands
    # per author rather than one combined \author{A \and B} block — check
    # this first, since the logic below would otherwise only ever see the
    # FIRST such command (find_all_commands returns one match per distinct
    # command occurrence, but the block-splitting logic below assumes all
    # authors live inside a single \author{...} argument).
    sn_authors = parse_sn_style_authors(preamble)
    if sn_authors:
        return sn_authors, None

    raw_author_blocks = find_all_commands(preamble, "author")
    authors = []

    if raw_author_blocks:
        block = raw_author_blocks[0]
        # IEEE style
        ieee_names = re.findall(r'\\IEEEauthorblockN\{(.*?)\}', block, re.DOTALL)
        ieee_affils = re.findall(r'\\IEEEauthorblockA\{(.*?)\}', block, re.DOTALL)
        if ieee_names:
            for i, name in enumerate(ieee_names):
                affil = ieee_affils[i] if i < len(ieee_affils) else None
                authors.append({"name": name.strip(), "affiliation":
                                 (affil.replace("\\\\", ", ").strip() if affil else None),
                                 "email": None})
        else:
            thanks_authors = parse_ieee_thanks_style(block)
            if thanks_authors:
                authors = thanks_authors
            else:
                # split on \and for multi-author plain style
                parts = re.split(r'\\and', block)
                for p in parts:
                    p_clean = re.sub(r'\\\w+(\{.*?\})?', '', p).strip()
                    if p_clean:
                        authors.append({"name": p_clean, "affiliation": None, "email": None})

    return authors if authors else [{"name": "(unparsed — see raw_author_block)",
                                       "affiliation": None, "email": None}], raw_author_blocks[0] if raw_author_blocks else None


def parse_latex_to_ir(tex_path):
    tex_path = Path(tex_path)
    source_dir = tex_path.parent
    raw = tex_path.read_text(encoding="utf-8", errors="ignore")
    text = strip_comments(raw)

    review_notes = []

    # Split preamble (before \begin{document}) vs body
    doc_match = re.search(r'\\begin\{document\}(.*)\\end\{document\}', text, re.DOTALL)
    if not doc_match:
        review_notes.append("Could not find \\begin{document}...\\end{document}; "
                             "treated entire file as body. Manual check required.")
        preamble = ""
        body = text
    else:
        preamble = text[:doc_match.start()]
        body = doc_match.group(1)

    # Pull in any \input{}/\include{}'d sub-files (common in multi-file
    # Overleaf projects: separate files per section, a macros.tex, etc.)
    # This applies to BOTH the preamble (macros.tex is often \input there)
    # and the body (section files).
    preamble = resolve_input_includes(preamble, source_dir)
    body = resolve_input_includes(body, source_dir)
    if "% [could not resolve" in preamble or "% [could not resolve" in body:
        review_notes.append("One or more \\input{}/\\include{} files referenced in the "
                             "project could not be found — check for missing sub-files.")

    custom_macros = extract_custom_macros(preamble)

    documentclass = extract_command_arg(text, "documentclass")

    title = extract_command_arg(body, "title") or extract_command_arg(preamble, "title")
    if not title:
        review_notes.append("No \\title{} found — check manually.")

    authors, raw_author_block = parse_authors_generic(body if "\\author" in body else preamble)

    # LNCS/Springer style links authors to affiliations by \inst{N} index via a
    # separate \institute{} block, rather than one affiliation per author inline
    # (which is what IEEE/BMC expect). We can't safely auto-map N -> affiliation
    # text, so surface the raw block and flag it instead of silently losing it.
    raw_institute_block = extract_command_arg(body, "institute") or extract_command_arg(preamble, "institute")
    if raw_institute_block:
        review_notes.append("Source used \\institute{} with \\inst{N} index references "
                             "(Springer/LNCS style) to link authors to affiliations. "
                             "This numbering scheme doesn't map directly to the target "
                             "template — affiliations were NOT auto-assigned to authors. "
                             "The original institute list is included below; assign it "
                             "manually: " + raw_institute_block.replace("\n", " "))

    abstract, _ = extract_env(body, "abstract")
    abstract_was_command = False
    if not abstract:
        # Some classes (Springer Nature's sn-jnl among them) use \abstract{...}
        # as a plain command rather than an environment — check that too,
        # so a source using this style doesn't leak untouched into the output.
        abstract = extract_command_arg(body, "abstract") or extract_command_arg(preamble, "abstract")
        if abstract:
            abstract_was_command = True
    if not abstract:
        review_notes.append("No abstract environment or \\abstract{} command found — check for a "
                             "non-standard abstract format in this source.")

    keywords = extract_command_arg(body, "keywords") or extract_command_arg(preamble, "keywords")
    if not keywords:
        # IEEE uses \begin{IEEEkeywords}...\end{IEEEkeywords}; Elsevier-family
        # classes use \begin{keyword}...\end{keyword} (singular, \sep-separated).
        # Without checking these, a source using either would leak its
        # class-specific environment syntax straight into the new output.
        ieee_kw, _ = extract_env(body, "IEEEkeywords")
        if ieee_kw:
            keywords = ieee_kw
        else:
            elsevier_kw, _ = extract_env(body, "keyword")
            if elsevier_kw:
                keywords = re.sub(r'\s*\\sep\s*', ', ', elsevier_kw).strip()
    if abstract and keywords and keywords in abstract:
        # Springer/LNCS conventionally places \keywords{} INSIDE the abstract
        # environment. Strip it out of the abstract text so it isn't duplicated
        # both there and in a separate keywords line in the output.
        abstract = remove_balanced_command(abstract, "keywords").strip()
    if keywords:
        # Springer/LNCS commonly separates keywords with "\and"; that's meaningless
        # outside their macro context, so normalize to a comma-separated list, which
        # every other target template (including IEEE's IEEEkeywords) expects.
        keywords = re.sub(r'\s*\\and\s*', ', ', keywords).strip().rstrip('.').strip()

    figures = extract_figures(body)
    tables = extract_tables(body)
    biblio = extract_bibliography(text, source_dir)

    if not biblio["bib_content"] and not biblio["inline_bibliography"]:
        review_notes.append("No .bib file found alongside the .tex and no inline "
                             "thebibliography block detected — bibliography must be "
                             "attached manually.")

    # Remove abstract/figures/tables content isn't stripped from section splitting;
    # that's fine, section text will still contain figure/table environments inline,
    # which is what we want for round-tripping content faithfully.
    # But strip the abstract block out of body before section-splitting so it's not
    # duplicated as a "preamble body" section.
    body_wo_abstract = re.sub(r'\\begin\{abstract\*?\}.*?\\end\{abstract\*?\}', '',
                               body, flags=re.DOTALL)
    # Also strip a command-style \abstract{...} (sn-jnl style) — same reasoning
    # as above, just a different syntax to remove so it doesn't leak through.
    if abstract_was_command:
        body_wo_abstract = remove_balanced_command(body_wo_abstract, "abstract")
    # Strip whichever keywords environment the source actually used, so its
    # class-specific syntax (IEEEkeywords, keyword) doesn't leak into output
    # built for a different target class that doesn't define it.
    body_wo_abstract = re.sub(r'\\begin\{IEEEkeywords\}.*?\\end\{IEEEkeywords\}', '',
                               body_wo_abstract, flags=re.DOTALL)
    body_wo_abstract = re.sub(r'\\begin\{keyword\}.*?\\end\{keyword\}', '',
                               body_wo_abstract, flags=re.DOTALL)
    # Also drop \maketitle, \IEEEauthorblock defs etc. — keep it minimal, just maketitle
    body_wo_abstract = body_wo_abstract.replace("\\maketitle", "")

    # Strip title/author/affiliation-block commands from the body so they don't
    # get captured as stray "content before first \section" and duplicated —
    # they're already extracted into the IR fields above and re-emitted by the
    # renderer using the target template's own author/title commands.
    for cmd in ["title", "author", "authorrunning", "institute", "keywords", "affil", "email"]:
        body_wo_abstract = remove_balanced_command(body_wo_abstract, cmd)

    # Strip trailing bibliography commands from body — they're handled separately
    # via the "bibliography" IR field and re-emitted by the renderer. Left in place,
    # they'd get captured as part of the last section's content and duplicated.
    body_wo_abstract = re.sub(r'\\bibliographystyle\{.*?\}', '', body_wo_abstract)
    body_wo_abstract = re.sub(r'\\bibliography\{.*?\}', '', body_wo_abstract)
    body_wo_abstract = re.sub(r'\\begin\{thebibliography\}.*?\\end\{thebibliography\}',
                               '', body_wo_abstract, flags=re.DOTALL)

    sections = split_sections(body_wo_abstract)

    # Collect asset file references (images + bib) so a project-level tool
    # can copy exactly the files this paper actually needs.
    image_paths = find_all_commands(body, "includegraphics")
    asset_files = {
        "images": sorted(set(image_paths)),
        "bib_files": biblio.get("bib_files", []),
    }

    ir = {
        "source_file": str(tex_path),
        "source_documentclass": documentclass,
        "title": title,
        "authors": authors,
        "raw_author_block": raw_author_block,
        "abstract": abstract,
        "keywords": keywords,
        "sections": sections,
        "figures": figures,
        "tables": tables,
        "bibliography": biblio,
        "custom_macros": custom_macros,
        "asset_files": asset_files,
        "raw_institute_block": raw_institute_block,
        "review_notes": review_notes,
    }
    return ir


def main():
    ap = argparse.ArgumentParser(description="Parse a LaTeX paper into a template-agnostic IR (JSON).")
    ap.add_argument("input", help="Path to source .tex file")
    ap.add_argument("-o", "--output", default=None, help="Output JSON path (default: <input>.ir.json)")
    args = ap.parse_args()

    ir = parse_latex_to_ir(args.input)
    out_path = args.output or (str(Path(args.input).with_suffix("")) + ".ir.json")
    Path(out_path).write_text(json.dumps(ir, indent=2), encoding="utf-8")
    print(f"Wrote IR to {out_path}")
    if ir["review_notes"]:
        print("\nReview notes:")
        for n in ir["review_notes"]:
            print(f"  - {n}")


if __name__ == "__main__":
    sys.exit(main())
