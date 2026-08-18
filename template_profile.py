"""
template_profile.py
--------------------
Given an extracted TARGET template project (e.g. what you get from
Overleaf's "Open as template" -> download zip, before you've written
anything into it — still has its sample title/author/abstract content),
this module reads that sample content and derives a "profile" describing
how the target journal expects a paper to be structured.

The profile has the SAME SHAPE as the hand-written JSON configs in
templates/*.json (see bmc.json / ieee.json / springer.json), so
render_template.render() can consume either one interchangeably. This
means the tool is no longer limited to the three journals someone
hand-configured — it works for any journal Overleaf has a template for.

What gets detected from the sample .tex:
  - documentclass line (copied verbatim, options and all)
  - preamble \\usepackage lines (copied verbatim)
  - author block style: IEEE's \\IEEEauthorblockN/A, LNCS/Springer's
    \\inst{N} indexing, or a generic \\author{}+\\address{} fallback
  - keywords style: \\begin{IEEEkeywords}, Elsevier's \\begin{keyword},
    or a plain \\keywords{} command
  - whether the abstract demo uses structured subheadings (e.g. BMC's
    Background/Methods/Results/Conclusions), and what those headers are
  - declaration sections present in the sample (Funding, Competing
    Interests, Data Availability, etc.) — matched against a broad list
    of common phrasings, not tied to any one publisher
  - bibliography style (\\bibliographystyle{...})
  - whether the layout is two-column (affects nothing in rendering
    directly, but is surfaced in notes since it affects figure sizing)

It also returns the list of support files (.cls/.sty/.bst/.clo) found in
the template project, which convert_project.py copies into the output
zip so the result is self-contained and doesn't require the user to
hunt down the class file separately.
"""

import re
from pathlib import Path

from latex_parser import (
    extract_command_arg, extract_env, remove_balanced_command,
    strip_comments, find_main_tex,
)

DECLARATION_PHRASES = [
    "Ethics approval and consent to participate",
    "Consent for publication",
    "Availability of data and materials",
    "Availability of data and code",
    "Data Availability Statement",
    "Data availability",
    "Competing interests",
    "Conflicts of Interest",
    "Conflict of interest",
    "Funding",
    "Acknowledgements",
    "Acknowledgments",
    "Authors' contributions",
    "Author contributions",
    "CRediT authorship contribution statement",
]

SUPPORT_FILE_EXTS = {".cls", ".sty", ".bst", ".clo"}


def detect_author_style(body):
    """Returns one of: 'ieee', 'inst', 'sn', 'generic'."""
    if "\\IEEEauthorblockN" in body:
        return "ieee"
    # Springer Nature's sn-jnl.cls: \author*[N]{\fnm{}\sur{}} + \affil*[N]{\orgname{}}
    if re.search(r'\\fnm\{', body) or re.search(r'\\orgdiv\{', body) or re.search(r'\\orgname\{', body):
        return "sn"
    if re.search(r'\\inst\{', body):
        return "inst"
    return "generic"


def detect_keywords_style(body):
    """Returns one of: 'ieeekeywords_env', 'keyword_env', 'keywords_cmd', None."""
    if re.search(r'\\begin\{IEEEkeywords\}', body):
        return "ieeekeywords_env"
    if re.search(r'\\begin\{keyword\}', body):
        return "keyword_env"
    if re.search(r'\\keywords\{', body):
        return "keywords_cmd"
    return None


def detect_abstract_style(body):
    """Returns 'command' if the sample uses \\abstract{...} as a plain command
    (used by sn-jnl and some other Springer/Elsevier-family classes), or
    'environment' for the more common \\begin{abstract}...\\end{abstract}.
    Checking this matters: emitting the wrong form can produce a broken or
    unheaded abstract if the target class doesn't define the other one."""
    if re.search(r'\\begin\{abstract\*?\}', body):
        return "environment"
    if re.search(r'\\abstract\{', body):
        return "command"
    return "environment"  # default assumption if neither was found in the sample


def detect_abstract_subheadings(body):
    """
    Looks inside the sample abstract (either \\begin{abstract}...\\end{abstract}
    or a plain \\abstract{...} command) for bolded subheadings (e.g.
    \\textbf{Background}: ...), which indicates the journal wants a
    structured abstract. Returns (requires, [headers]).
    """
    abstract_text, _ = extract_env(body, "abstract")
    if not abstract_text:
        abstract_text = extract_command_arg(body, "abstract")
    if not abstract_text:
        return False, []
    headers = re.findall(r'\\textbf\{([A-Z][A-Za-z][A-Za-z \-]{2,25})\}', abstract_text)
    seen, uniq = set(), []
    for h in headers:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return (len(uniq) >= 2), uniq


def get_section_titles(body):
    """Extract just the titles of all \\section{}/\\section*{} (top-level
    only) headings in body, for matching against known declaration phrases.
    Deliberately narrower than latex_parser.split_sections — we only need
    titles here, not content, and only want real headings, not any mention
    of a phrase in ordinary prose."""
    titles = []
    for m in re.finditer(r'\\section\*?\{', body):
        title = extract_command_arg(body[m.start():], "section")
        if title:
            titles.append(title)
    return titles


def detect_declaration_sections(body):
    """Match known declaration phrases against actual section TITLES only
    (not the whole body) so a phrase mentioned in ordinary prose doesn't
    create a false positive. Also de-duplicates near-identical matches
    (e.g. 'Data availability' vs 'Data Availability Statement' both
    matching the same heading) by keeping only the longest match per
    heading."""
    titles = get_section_titles(body)
    found = []
    for title in titles:
        title_matches = [p for p in DECLARATION_PHRASES
                          if re.search(re.escape(p), title, re.IGNORECASE)]
        if title_matches:
            # keep the longest matching phrase for this heading (most specific)
            found.append(max(title_matches, key=len))
    # de-dupe while preserving order (in case two headings matched the same phrase)
    seen, uniq = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def infer_bibliographystyle_from_options(documentclass_line, support_files):
    """
    Some classes (Springer Nature's sn-jnl among them) select the citation
    style via a documentclass OPTION rather than an explicit
    \\bibliographystyle{} command in the sample — e.g.
    \\documentclass[sn-mathphys-num]{sn-jnl} paired with a bundled
    sn-mathphys-num.bst. If no explicit \\bibliographystyle was found, check
    whether any class option matches the name of a bundled .bst file and use
    that instead of silently defaulting to "plain".
    """
    if not documentclass_line:
        return None
    opts_match = re.search(r'\\documentclass\[([^\]]*)\]', documentclass_line)
    if not opts_match:
        return None
    options = [o.strip() for o in opts_match.group(1).split(",")]
    bst_stems = {Path(p).stem for p in support_files if Path(p).suffix.lower() == ".bst"}
    for opt in options:
        if opt in bst_stems:
            return opt
    return None


def detect_documentclass_line(raw_text):
    m = re.search(r'\\documentclass(\[[^\]]*\])?\{[^}]*\}', raw_text)
    return m.group(0) if m else None


def detect_preamble_packages(preamble_text):
    lines = re.findall(r'\\(?:usepackage|RequirePackage)(?:\[[^\]]*\])?\{[^}]*\}', preamble_text)
    # de-dupe while preserving order
    seen, uniq = set(), []
    for l in lines:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    return "\n".join(uniq)


def detect_twocolumn(documentclass_line, preamble_text):
    if documentclass_line and "twocolumn" in documentclass_line:
        return True
    if "\\twocolumn" in preamble_text:
        return True
    # IEEEtran defaults to two-column for conference/journal styles
    if documentclass_line and "IEEEtran" in documentclass_line:
        return True
    return False


def find_support_files(project_dir):
    """Relative paths (within project_dir) of .cls/.sty/.bst/.clo files."""
    out = []
    for p in Path(project_dir).rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORT_FILE_EXTS:
            out.append(p.relative_to(project_dir))
    return out


def analyze_template_dir(project_dir, main_tex_override=None):
    """
    Main entry point. Returns (profile_dict, support_file_paths, notes).
    profile_dict has the same shape as templates/*.json so it's a drop-in
    replacement for render_template.render()'s `target` argument.
    """
    notes = []
    project_dir = Path(project_dir)

    if main_tex_override:
        main_tex_path = project_dir / main_tex_override
    else:
        main_tex_path, candidates = find_main_tex(project_dir)
        if not main_tex_path:
            raise ValueError(
                "Could not find a sample .tex in the target template project "
                "(needs both \\documentclass and \\begin{document}). If the "
                "template zip has an unusual layout, this template can't be "
                "auto-analyzed — fall back to a hand-written templates/*.json "
                "config instead."
            )
        if len(candidates) > 1:
            notes.append(f"{len(candidates)} candidate main files found in the target "
                          f"template project; used {main_tex_path.name}.")

    raw = main_tex_path.read_text(encoding="utf-8", errors="ignore")
    text = strip_comments(raw)

    doc_match = re.search(r'\\begin\{document\}(.*)\\end\{document\}', text, re.DOTALL)
    if doc_match:
        preamble = text[:doc_match.start()]
        body = doc_match.group(1)
    else:
        preamble = text
        body = text
        notes.append("Target template sample had no \\begin{document}...\\end{document} — "
                      "structure detection may be unreliable, double-check the output.")

    documentclass_line = detect_documentclass_line(text)
    if not documentclass_line:
        raise ValueError("Could not find \\documentclass in the target template's sample file.")

    preamble_packages = detect_preamble_packages(preamble)
    # \author{}/\institute{} commonly sit in the PREAMBLE for Springer/LNCS-
    # style classes (before \begin{document}), so check both, not just body.
    full_text_for_structure = preamble + "\n" + body
    author_style = detect_author_style(full_text_for_structure)
    if author_style == "generic" and documentclass_line and "sn-jnl" in documentclass_line:
        # Springer Nature's class — trust the documentclass name even if the
        # sample didn't happen to show \fnm{}/\orgdiv{} explicitly.
        author_style = "sn"
        notes.append("Target documentclass is sn-jnl (Springer Nature) but the sample "
                      "didn't show \\fnm{}/\\orgdiv{} macros directly — using the sn-jnl "
                      "author renderer based on the class name; double-check the author "
                      "block syntax against the sample if this looks off.")
    keywords_style = detect_keywords_style(full_text_for_structure)
    abstract_requires_subheadings, abstract_subheadings = detect_abstract_subheadings(full_text_for_structure)
    abstract_style = detect_abstract_style(full_text_for_structure)
    declaration_sections = detect_declaration_sections(body)
    support_files = find_support_files(project_dir)

    bibliographystyle = extract_command_arg(full_text_for_structure, "bibliographystyle")
    if not bibliographystyle:
        inferred = infer_bibliographystyle_from_options(documentclass_line, support_files)
        if inferred:
            bibliographystyle = inferred
            notes.append(f"No explicit \\bibliographystyle{{}} found in the sample — inferred "
                          f"'{inferred}' from a matching documentclass option and bundled .bst "
                          f"file. Verify this is the right citation style for your target journal.")
        else:
            bibliographystyle = "plain"
            notes.append("No \\bibliographystyle{} found in the sample and no documentclass "
                          "option matched a bundled .bst file — defaulted to 'plain'. Check "
                          "the sample template for the correct citation style.")

    twocolumn = detect_twocolumn(documentclass_line, preamble)

    if not support_files:
        notes.append("No .cls/.sty/.bst files found in the target template project — "
                      "make sure you downloaded the FULL template zip (not just the .tex), "
                      "or the output may not compile without fetching the class file separately.")

    if author_style == "generic":
        notes.append("Could not detect a specific author-block macro style (IEEE-style, "
                      "LNCS \\inst{}-style, or Springer Nature's \\fnm{}/\\orgdiv{}-style) in "
                      "the target template's sample — using a generic "
                      "\\author{}/\\address{} fallback. Check the sample template's own author "
                      "block manually and adjust if this target class expects something else.")

    if twocolumn:
        notes.append("Target layout is two-column. Wide/dense figures and tables from the "
                      "source may need figure*/table* (double-column-spanning) to stay legible "
                      "— check figure widths after conversion, especially multi-panel plots.")

    profile = {
        "name": f"Auto-detected from {main_tex_path.name}",
        "documentclass": documentclass_line,
        "preamble_extra": preamble_packages,
        "requires_declarations": bool(declaration_sections),
        "declaration_sections": declaration_sections,
        "abstract_requires_subheadings": abstract_requires_subheadings,
        "abstract_subheadings": abstract_subheadings,
        "abstract_style": abstract_style,
        "bibliographystyle": bibliographystyle,
        "author_style": author_style,
        "keywords_style": keywords_style,
        "twocolumn": twocolumn,
        "notes": notes,
    }
    return profile, support_files, notes
