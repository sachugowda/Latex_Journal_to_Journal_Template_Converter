"""
render_template.py
-------------------
Takes the template-agnostic IR (produced by latex_parser.py) and a target
template profile (produced by template_profile.py from an empty target
template zip) and emits:
  1. A new .tex file structured for the target template.
  2. A REVIEW_CHECKLIST.md listing everything a human must verify —
     this tool does NOT claim to produce a submission-ready file, only a
     correctly-structured draft that saves the mechanical rebuilding work.

This module is a library used by convert_project.py — it has no standalone
CLI of its own; run convert_project.py instead.
"""



def render_authors_ieee(authors):
    if not authors:
        return "% TODO: no authors were detected from the source — add manually.\n\\author{[Author Name]}"
    lines = ["\\author{"]
    for a in authors:
        lines.append("\\IEEEauthorblockN{%s}" % a["name"])
        if a.get("affiliation"):
            lines.append("\\IEEEauthorblockA{%s}" % a["affiliation"])
        else:
            lines.append("% TODO: affiliation not detected from source for this author")
    lines.append("}")
    return "\n".join(lines)


def affil_list_for(a):
    """Returns an author's affiliations as a list (using the "affiliations"
    field when present for multi-institution authors, else falling back to
    the single "affiliation" string), or None if neither is set."""
    if a.get("affiliations"):
        return a["affiliations"]
    aff = a.get("affiliation")
    return [aff] if aff else None


def build_affil_numbering(authors, missing_placeholder):
    """Shared numbering pass for \\inst{N}/\\affil[N]-style targets: assigns
    each distinct affiliation string a number (shared across authors with
    the exact same text), in order of first appearance. Returns
    (affil_to_num dict, order list, missing_affil bool)."""
    affil_to_num = {}
    order = []
    missing_affil = False
    for a in authors:
        affs = affil_list_for(a)
        if not affs:
            missing_affil = True
            affs = [missing_placeholder]
        for aff in affs:
            if aff not in affil_to_num:
                affil_to_num[aff] = len(affil_to_num) + 1
                order.append(aff)
    return affil_to_num, order, missing_affil


def render_authors_inst(authors):
    """LNCS/Springer style: authors linked to a numbered \\institute{} list
    via \\inst{N}. Each author gets one number per distinct affiliation they
    have (an author with two institutions gets e.g. \\inst{1,2}); authors
    sharing the same affiliation text share a number."""
    if not authors:
        return "% TODO: no authors were detected from the source — add manually.\n\\author{[Author Name]\\inst{1}}\n\\institute{[Affiliation]}"
    affil_to_num, order, missing_affil = build_affil_numbering(
        authors, "[Affiliation not detected — fill in manually]")
    author_parts = []
    for a in authors:
        affs = affil_list_for(a) or [order[0]]
        nums = ",".join(str(affil_to_num[aff]) for aff in affs)
        author_parts.append(f"{a['name']}\\inst{{{nums}}}")
    lines = []
    if missing_affil:
        lines.append("% TODO: one or more authors had no affiliation detected from the source — fill in manually.")
    lines.append("\\author{" + " \\and\n".join(author_parts) + "}")
    lines.append("\\institute{" + " \\and\n".join(order) + "}")
    return "\n".join(lines)


def split_name_for_sn(full_name):
    """Best-effort first/last name split for Springer Nature's \\fnm{}/\\sur{}
    convention, which expects them separate. Splits on the last whitespace —
    works for most Western name orders but not reliably for multi-word
    surnames (e.g. 'van der Berg') or single-name authors; flagged in notes."""
    parts = full_name.strip().rsplit(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", full_name


def render_authors_sn(authors):
    """Springer Nature (sn-jnl.cls) style: \\author*[N]{\\fnm{}\\sur{}}\\email{}
    plus a numbered \\affil*[N]{\\orgname{}} list. Institute numbering matches
    across authors who share the exact same affiliation text, and an author
    with multiple institutions gets multiple numbers (e.g. [1,2]). The first
    author is marked corresponding (*).
    Address fields (orgdiv/orgaddress) aren't reliably parseable from a
    single free-text affiliation string, so each affiliation goes into a
    single \\orgname{} field and is flagged for the user to split out
    manually."""
    if not authors:
        return ("% TODO: no authors were detected from the source — add manually.\n"
                "\\author*[1]{\\fnm{[First]} \\sur{[Last]}}\n"
                "\\affil*[1]{\\orgname{[Affiliation]}}")

    affil_to_num, order, missing_affil = build_affil_numbering(
        authors, "[Affiliation not detected — fill in manually]")

    lines = []
    if missing_affil:
        lines.append("% TODO: one or more authors had no affiliation detected from the source — fill in manually.")
    lines.append("% NOTE: first/last name split below is best-effort (splits on last space) — "
                  "verify for compound surnames.")
    for i, a in enumerate(authors):
        affs = affil_list_for(a) or [order[0]]
        num_str = ",".join(str(affil_to_num[aff]) for aff in affs)
        fnm, sur = split_name_for_sn(a["name"])
        star = "*" if i == 0 else ""
        line = f"\\author{star}[{num_str}]{{\\fnm{{{fnm}}} \\sur{{{sur}}}}}"
        if a.get("email"):
            line += f"\\email{{{a['email']}}}"
        lines.append(line)
    for aff, num in affil_to_num.items():
        star = "*" if num == 1 else ""
        lines.append(f"\\affil{star}[{num}]{{\\orgname{{{aff}}}}}")
    return "\n".join(lines)


def render_authors_generic(authors):
    """Fallback: simple \\author{} + \\address{} pairs (bmcart-style)."""
    if not authors:
        return "% TODO: no authors were detected from the source — add manually.\n\\author{[Author Name]}\n\\address{[Affiliation]}"
    parts = []
    for a in authors:
        parts.append("\\author{%s}" % a["name"])
        if a.get("affiliation"):
            parts.append("\\address{%s}" % a["affiliation"])
        else:
            parts.append("% TODO: affiliation not detected from source for this author")
            parts.append("\\address{[Affiliation]}")
        if a.get("email"):
            parts.append("\\email{%s}" % a["email"])
    return "\n".join(parts)


def render_abstract(ir, target):
    abstract_text = ir.get("abstract") or "TODO: abstract text was not detected in the source file."
    if target.get("abstract_requires_subheadings"):
        # Best effort: if the source abstract doesn't already contain the required
        # subheadings, wrap the whole thing under the first one and leave placeholders
        # for the rest, clearly flagged.
        headers = target["abstract_subheadings"]
        already_structured = all(h.lower() in abstract_text.lower() for h in headers)
        if already_structured:
            body = abstract_text
        else:
            body_lines = [f"\\textbf{{{headers[0]}}}: {abstract_text}"]
            for h in headers[1:]:
                body_lines.append(f"\\textbf{{{h}}}: TODO — split out from source abstract "
                                   f"(source did not use structured subheadings).")
            body = "\n\n".join(body_lines)
    else:
        body = abstract_text

    if target.get("abstract_style") == "command":
        # Some classes (e.g. Springer Nature's sn-jnl) define \abstract{...}
        # as a plain command, not a \begin{abstract}...\end{abstract}
        # environment — emitting the environment form when the class doesn't
        # define it can silently misrender (no heading, garbled placement).
        return "\\abstract{%s}" % body
    return "\\begin{abstract}\n%s\n\\end{abstract}" % body


def render_sections(sections):
    out = []
    level_cmd = {0: None, 1: "section", 2: "subsection", 3: "subsubsection"}
    for s in sections:
        if s["level"] == 0:
            # stray content before first heading (rare after abstract stripped) — keep, flagged
            if s["content"].strip():
                out.append("% --- content found before first \\section; verify placement ---")
                out.append(s["content"])
        else:
            cmd = level_cmd[s["level"]]
            out.append(f"\\{cmd}{{{s['title']}}}")
            out.append(s["content"])
        out.append("")
    return "\n".join(out)


def render_declarations(target):
    if not target.get("requires_declarations"):
        return ""
    lines = ["% ==== Journal-required declaration sections (fill in before submission) ===="]
    for sec in target["declaration_sections"]:
        lines.append(f"\\section*{{{sec}}}")
        lines.append("TODO: fill in.")
        lines.append("")
    return "\n".join(lines)


def render_bibliography(ir, target):
    biblio = ir["bibliography"]
    style = target.get("bibliographystyle", "plain")
    lines = [f"\\bibliographystyle{{{style}}}"]
    if biblio.get("bib_files"):
        names = ",".join(biblio["bib_files"])
        lines.append(f"\\bibliography{{{names}}}")
        lines.append("% NOTE: re-using the source .bib file — entry TYPES are usually "
                      "compatible across styles, but re-run BibTeX/Biber and check for "
                      "warnings about missing required fields for the new style.")
    elif biblio.get("inline_bibliography"):
        lines.append("\\begin{thebibliography}{99}")
        lines.append(biblio["inline_bibliography"])
        lines.append("\\end{thebibliography}")
    else:
        lines.append("% TODO: no bibliography source was found — attach your .bib file "
                      "or thebibliography block manually.")
    return "\n".join(lines)


def render_keywords(ir, target):
    """Returns a list of lines for the keywords block, or [] if no keywords."""
    if not ir.get("keywords"):
        return []
    keywords_style = target.get("keywords_style")
    lines = []
    if keywords_style == "ieeekeywords_env":
        lines.append("\\begin{IEEEkeywords}")
        lines.append(ir['keywords'])
        lines.append("\\end{IEEEkeywords}")
    elif keywords_style == "keyword_env":
        lines.append("\\begin{keyword}")
        lines.append(ir['keywords'].replace(", ", " \\sep "))
        lines.append("\\end{keyword}")
    else:
        lines.append(f"\\keywords{{{ir['keywords']}}}")
    lines.append("")
    return lines


def render(ir, target):
    parts = []
    parts.append(target["documentclass"])
    if target.get("preamble_extra"):
        parts.append(target["preamble_extra"])
    if ir.get("custom_macros"):
        parts.append("")
        parts.append("% ---- custom macros carried over from the source project ----")
        parts.append("% Verify these don't clash with macros already defined by the "
                      "target document class.")
        parts.extend(ir["custom_macros"])
    parts.append("")
    if not ir.get("title"):
        parts.append("% TODO: no title was detected from the source — add manually.")
    parts.append(f"\\title{{{ir.get('title') or '[Paper Title]'}}}")
    parts.append("")

    author_style = target.get("author_style")

    if author_style == "ieee":
        parts.append(render_authors_ieee(ir["authors"]))
    elif author_style == "inst":
        parts.append(render_authors_inst(ir["authors"]))
    elif author_style == "sn":
        parts.append(render_authors_sn(ir["authors"]))
    else:
        parts.append(render_authors_generic(ir["authors"]))

    # If the SOURCE used \institute{}/\inst{} but the TARGET does not
    # (author_style not in {"inst", "sn"}, both of which consume it), the
    # institute list can't be auto-assigned — surface it as a comment.
    if ir.get("raw_institute_block") and author_style not in ("inst", "sn"):
        parts.append("% TODO: source used \\institute{} with \\inst{N} indices to link "
                      "authors to affiliations — assign these manually per author:")
        for line in ir["raw_institute_block"].strip().split("\n"):
            parts.append("% " + line.strip())
    parts.append("")

    abstract_block = render_abstract(ir, target)
    keyword_lines = render_keywords(ir, target)

    if target.get("abstract_style") == "command":
        # Classes like sn-jnl declare \abstract{...} (and usually \keywords{})
        # as FRONTMATTER — before \maketitle, alongside \title/\author — not
        # after it like a \begin{abstract} environment. Placing it after
        # \maketitle here would mean the class never picks it up for the
        # typeset title/abstract block.
        parts.append(abstract_block)
        parts.append("")
        parts.extend(keyword_lines)
        parts.append("\\begin{document}")
        parts.append("\\maketitle")
        parts.append("")
    else:
        parts.append("\\begin{document}")
        parts.append("\\maketitle")
        parts.append("")
        parts.append(abstract_block)
        parts.append("")
        parts.extend(keyword_lines)

    parts.append(render_sections(ir["sections"]))

    decl = render_declarations(target)
    if decl:
        parts.append(decl)

    parts.append(render_bibliography(ir, target))
    parts.append("")
    parts.append("\\end{document}")

    return "\n".join(parts)


def build_checklist(ir, target):
    lines = [f"# Review checklist — converted to {target['name']}", ""]
    lines.append("This file was mechanically restructured. Before submitting, verify:")
    lines.append("")
    lines.append("## From the source-parsing step")
    if ir["review_notes"]:
        for n in ir["review_notes"]:
            lines.append(f"- [ ] {n}")
    else:
        lines.append("- [x] No parsing issues flagged.")
    lines.append("")
    lines.append("## From the target template")
    for n in target.get("notes", []):
        lines.append(f"- [ ] {n}")
    lines.append("")
    lines.append("## Always check manually")
    lines.append("- [ ] Author names/affiliations rendered correctly (author block parsing is "
                 "heuristic and template-dependent — this is the most error-prone step).")
    lines.append("- [ ] Figures/tables: paths, captions, and placement (`\\includegraphics` "
                 "paths carry over as-is; confirm image files are in the new project).")
    lines.append("- [ ] Math macros defined with \\newcommand in the old preamble were **not** "
                 "auto-migrated — copy any custom macros into the new preamble.")
    lines.append("- [ ] Bibliography style compiles cleanly (re-run BibTeX/Biber and check the "
                 "log for missing-field warnings under the new style).")
    lines.append("- [ ] Word count / reference count / section requirements for the exact "
                 "target journal (these change over time and aren't hard-coded in this tool).")
    lines.append("- [ ] Abstract structure matches what the target journal currently requires.")
    return "\n".join(lines)
