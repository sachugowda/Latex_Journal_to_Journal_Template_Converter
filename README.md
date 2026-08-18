# LaTeX Journal Template Converter

Converts a LaTeX paper written for one journal's template into another —
for reuse after a rejection, or when re-targeting a paper to a different
venue. Works with whole Overleaf projects (multi-file, figures, `.bib`)
and any target journal, not a fixed list — it reads the target's own
template to figure out how to structure the output.

**What this does:** extracts your paper's content (title, authors, abstract,
sections, figures, tables, bibliography) into a template-agnostic
intermediate format, then re-emits it using the target template's structure
and commands, adding placeholders for any journal-required sections your
source didn't have (e.g. "Data Availability", "Competing Interests").

**What this does NOT do:** guarantee a submission-ready file. LaTeX journal
classes have too many idiosyncrasies (custom macros, exact word/reference
limits, house style rules that change over time) for any script to promise
100% automated conversion. Every run produces a `REVIEW_CHECKLIST.md`
alongside the output — treat that as required reading, not boilerplate.

## Requirements

Python 3.8+. No external dependencies (standard library only).

## Usage

Point `--input` at your source project and `--output-template` at an
**empty/unmodified** template project for the target journal — Overleaf:
open the target journal's official template (gallery or the journal's own
submission-guidelines page), download as zip **before writing anything
into it**, so it still has its own sample title/author/abstract content.

```bash
python3 convert_project.py --input my_source_project.zip \
                            --output-template empty_journal_template.zip \
                            --output my_paper_converted.zip
```

The tool reads that sample content to work out how the target journal
expects things structured — documentclass, author-block style (IEEE's
`\IEEEauthorblockN/A`, LNCS/Springer's `\inst{N}` indexing, Springer
Nature's `\fnm{}/\sur{}/\orgdiv{}` convention, or a generic
`\author{}/\address{}` fallback), whether the abstract is a
`\begin{abstract}` environment or a plain `\abstract{}` command (matters —
some classes only define one of the two), keyword syntax, declaration
sections it requires (detected from real section headings in the sample,
not a fixed list), and bibliography style. It also copies the target's own
`.cls`/`.sty`/`.bst` files into the output zip, so the result is
self-contained.

**Handles:**
- Multi-file source projects (`\input{}` / `\include{}`'d section files, a
  separate `macros.tex`, etc.) — everything gets merged before conversion.
- Custom `\newcommand`/`\newenvironment` macros — detected and carried over
  into the new preamble automatically.
- Figures and `.bib` files — copied into the output zip at the same
  relative paths your `\includegraphics`/`\bibliography` commands expect.
- Auto-detects the main `.tex` file in both the source and target
  template projects. Override with `--main-tex` (source) /
  `--output-template-main-tex` (target) if it picks the wrong one.

Output zip contains: `main.tex` (converted), all referenced figures/bib
files, the target's `.cls`/`.sty`/`.bst` files, `REVIEW_CHECKLIST.md`,
`CONVERSION_NOTES.txt`, and the intermediate JSON
(`_parsed_content.ir.json`) for transparency if something looks off.

**Known limits of the auto-detection:** it can only detect what's actually
demonstrated in the template's sample content — if the sample doesn't show
a structured abstract or doesn't list every declaration section the
journal actually requires, those won't be detected. Always cross-check
against the journal's current submission guidelines before submitting;
this tool saves the mechanical rebuilding work, it doesn't replace reading
the guidelines.

## Known limitations (be aware of these)

- Author/affiliation parsing is heuristic. It handles plain `\author{A \and B}`,
  IEEE's `\IEEEauthorblockN/A`, IEEE's `\thanks{}`-based per-group affiliation
  style (plain-English "A, B, and C are with..." name lists, including
  braced multi-email shorthand like `\{a, b, c\}@domain` and authors with
  two institutions via "...and also with..."), LNCS/Springer's `\inst{N}`-
  indexed style (including multiple numbers per author, e.g. `\inst{1,2}`),
  and Springer Nature's `\fnm{}/\sur{}/\orgdiv{}` convention on BOTH the
  source and target sides (separate numbered `\author*[N]{}`/`\affil[N]{}`
  commands, multi-affiliation authors, per-author `\email{}`; first/last
  name split is a best-effort split on the last space — check compound
  surnames manually). Unusual macro-heavy author blocks may still need
  manual cleanup.
- The parser recognizes both common syntax variants for abstract
  (`\begin{abstract}` environment or a plain `\abstract{}` command) and
  keywords (`\keywords{}` command, IEEE's `\begin{IEEEkeywords}`
  environment, or Elsevier's `\begin{keyword}` environment) on the SOURCE
  side, and strips whichever one is actually used out of the body before
  conversion — so a source written in one journal's syntax doesn't leak
  that class-specific syntax untouched into a target that doesn't define it.
- When an author's affiliation genuinely can't be detected from the source,
  the tool emits a short `[Affiliation not detected]` bracket placeholder in
  the actual field (safe to compile, clearly visible to edit) plus a `%`
  comment with the full explanation — never a long TODO sentence inside a
  rendered command argument, since that can visibly break the compiled
  output's front matter.
- Auto-detection only knows what the target template's sample content
  actually demonstrates — a declaration section, structured-abstract
  format, or author style the sample doesn't show won't be detected. It
  scans both the preamble and body for author/keyword/abstract commands
  (some classes put these before `\begin{document}`, others after), but a
  genuinely unusual template layout can still be missed.
- Figures/tables are carried over as raw LaTeX blocks (paths, captions,
  labels preserved) rather than re-validated against the new template's
  column layout — dense multi-panel figures sized for a single-column
  source may need `figure*`/`table*` (double-column-spanning) in a
  two-column target to stay legible; check after converting.
- This tool does not know current journal-specific word/reference/page
  limits — those change over time and must be checked on the journal's site.

## Files

```
convert_project.py     - entry point: whole project .zip -> ready-to-upload .zip
latex_parser.py         - source .tex -> intermediate representation (also usable standalone)
template_profile.py     - analyzes an empty target template zip -> structure profile
render_template.py      - intermediate representation + profile -> target .tex + checklist
samples/                 - example multi-file IEEE project, for testing
```

`latex_parser.py` can also be run standalone for debugging (`python3
latex_parser.py mypaper.tex -o mypaper.ir.json` — dumps the parsed
intermediate representation without converting anything). `render_template.py`
and `template_profile.py` are library modules only, imported by
`convert_project.py`.
