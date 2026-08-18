"""
convert_project.py
-------------------
Takes a WHOLE LaTeX project as a .zip (an Overleaf "Download as zip", or any
offline project folder zipped up — main .tex, any \\input/\\include'd
sub-files, figures, .bib file(s)) and converts it to a target journal
template, producing a ready-to-upload .zip containing:

  - the converted main .tex file, restructured for the target template
  - all figure/image files the paper actually references, at the same
    relative paths so \\includegraphics keeps working
  - the .bib file(s) the paper references
  - the target template's own .cls/.sty/.bst files, so the result is
    self-contained
  - REVIEW_CHECKLIST.md — what to verify before submitting
  - CONVERSION_NOTES.txt — a plain-text summary of what was done

SPECIFYING THE TARGET: point --output-template at an EMPTY/UNMODIFIED
template project zip for the target journal (Overleaf: open the journal's
template, download as zip, before writing anything into it — still has the
sample title/author/abstract content). The tool reads that sample to figure
out the target's structure automatically (documentclass, author-block
style, abstract format, required declaration sections, bibliography style)
— works for any journal Overleaf has a template for.

Usage:
    python3 convert_project.py --input my_ieee_project.zip \\
                                --output-template empty_acm_template.zip \\
                                --output my_paper_acm_ready.zip

    # If either project has multiple .tex files and auto-detection picks
    # the wrong one as "main":
    python3 convert_project.py --input my_ieee_project.zip \\
                                --output-template empty_bmc_template.zip \\
                                --output out.zip \\
                                --main-tex subdir/paper.tex \\
                                --output-template-main-tex sample.tex
"""

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

import latex_parser
import render_template
import template_profile


def extract_zip(zip_path, dest_dir):
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(dest_dir)


def find_main_tex(project_dir):
    """Deprecated local copy — kept as a thin wrapper for backward compat.
    The real implementation now lives in latex_parser.py so both this
    module and template_profile.py share one definition."""
    return latex_parser.find_main_tex(project_dir)


def copy_assets(ir, project_dir, out_dir, review_notes):
    """Copy every image and .bib file the paper references into out_dir,
    preserving relative paths/extensions so \\includegraphics and
    \\bibliography keep working unmodified."""
    assets = ir.get("asset_files", {})
    project_dir = Path(project_dir)
    out_dir = Path(out_dir)

    image_exts = ["", ".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg", ".tif", ".tiff"]
    for img in assets.get("images", []):
        found = False
        for ext in image_exts:
            candidate = project_dir / (img + ext) if not img.endswith(tuple(image_exts[1:])) else project_dir / img
            matches = list(project_dir.rglob(Path(img).name + ext)) if ext else list(project_dir.rglob(Path(img).name + ".*"))
            search_paths = [candidate] + matches
            for sp in search_paths:
                if sp.exists() and sp.is_file():
                    rel = sp.relative_to(project_dir)
                    dest = out_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sp, dest)
                    found = True
                    break
            if found:
                break
        if not found:
            review_notes.append(f"Could not locate image file for \\includegraphics{{{img}}} "
                                 f"in the project — copy it in manually.")

    for bib in assets.get("bib_files", []):
        for candidate_name in [bib, bib + ".bib"]:
            matches = list(project_dir.rglob(candidate_name))
            if matches:
                sp = matches[0]
                rel = sp.relative_to(project_dir)
                dest = out_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sp, dest)
                break
        else:
            review_notes.append(f"Could not locate bib file '{bib}' in the project — "
                                 f"attach it manually.")


def build_conversion_notes(main_tex_path, all_candidates, ir, target):
    lines = []
    lines.append("LaTeX project conversion notes")
    lines.append("=" * 40)
    lines.append(f"Detected main file: {main_tex_path}")
    if len(all_candidates) > 1:
        lines.append(f"NOTE: {len(all_candidates)} files with \\documentclass+\\begin{{document}} "
                      f"were found in the project. If this picked the wrong one, re-run with "
                      f"--main-tex <path>. Candidates were:")
        for c in all_candidates:
            lines.append(f"  - {c}")
    lines.append(f"Target template: {target['name']}")
    lines.append(f"Source document class: {ir.get('source_documentclass')}")
    lines.append(f"Custom macros carried over: {len(ir.get('custom_macros', []))}")
    lines.append(f"Figures referenced: {len(ir.get('asset_files', {}).get('images', []))}")
    lines.append(f"Bib files referenced: {len(ir.get('asset_files', {}).get('bib_files', []))}")
    lines.append("")
    lines.append("Next step: create a new project on Overleaf from the journal's official "
                  "current template, then copy the converted .tex content and all asset "
                  "files from this zip into it. See REVIEW_CHECKLIST.md for what to verify.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Convert a whole LaTeX project zip into a target journal template, "
                    "producing a ready-to-upload zip with all assets attached.")
    ap.add_argument("--input", required=True, help="Path to input project .zip")
    ap.add_argument("--output-template", required=True,
                    help="Path to an EMPTY target template project .zip (e.g. downloaded "
                         "from Overleaf's gallery before writing anything into it). The "
                         "tool reads its sample content to auto-detect structure — "
                         "documentclass, author style, abstract format, declarations, "
                         "bibliography style — and copies its .cls/.sty/.bst files into "
                         "the output.")
    ap.add_argument("--output-template-main-tex", default=None,
                    help="Relative path (within --output-template's zip) to its sample "
                         "main .tex, if auto-detection picks the wrong one")
    ap.add_argument("--output", required=True, help="Path to write the output .zip")
    ap.add_argument("--main-tex", default=None,
                    help="Relative path (within --input's zip) to the main .tex file, "
                         "if auto-detection picks the wrong one")
    args = ap.parse_args()

    work_dir = Path("/tmp/latex_convert_work")
    if work_dir.exists():
        shutil.rmtree(work_dir)
    extract_dir = work_dir / "extracted"
    out_dir = work_dir / "output"
    extract_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)

    print(f"Extracting {args.input} ...")
    extract_zip(args.input, extract_dir)

    if args.main_tex:
        main_tex_path = extract_dir / args.main_tex
        all_candidates = [main_tex_path]
        if not main_tex_path.exists():
            print(f"ERROR: --main-tex path not found: {args.main_tex}")
            sys.exit(1)
    else:
        main_tex_path, all_candidates = find_main_tex(extract_dir)
        if not main_tex_path:
            print("ERROR: Could not find a main .tex file (needs both \\documentclass "
                  "and \\begin{document}). Use --main-tex to specify one explicitly.")
            sys.exit(1)
        print(f"Detected main file: {main_tex_path.relative_to(extract_dir)}")
        if len(all_candidates) > 1:
            print(f"  (note: {len(all_candidates)} candidate files found — "
                  f"use --main-tex to override if this is wrong)")

    template_extract_dir = work_dir / "target_template_extracted"
    template_extract_dir.mkdir(parents=True)
    print(f"Extracting target template {args.output_template} ...")
    extract_zip(args.output_template, template_extract_dir)
    print("Analyzing target template structure ...")
    try:
        target, support_file_rel_paths, profile_notes = template_profile.analyze_template_dir(
            template_extract_dir, main_tex_override=args.output_template_main_tex)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print(f"Detected target: documentclass={target['documentclass']}, "
          f"author_style={target['author_style']}, "
          f"keywords_style={target['keywords_style']}, "
          f"bibliographystyle={target['bibliographystyle']}")
    if target["declaration_sections"]:
        print(f"  Declaration sections found in template: "
              f"{', '.join(target['declaration_sections'])}")
    for n in profile_notes:
        print(f"  NOTE: {n}")
    # Copy the target's .cls/.sty/.bst files into the output, flattened to
    # the root so they sit alongside main.tex where LaTeX will find them.
    support_files_copied = []
    for rel_path in support_file_rel_paths:
        src = template_extract_dir / rel_path
        dest_name = Path(rel_path).name
        dest = out_dir / dest_name
        if dest.exists():
            print(f"  WARNING: duplicate support file name '{dest_name}' — "
                  f"keeping the first one found, check manually.")
            continue
        shutil.copy2(src, dest)
        support_files_copied.append(dest_name)
    if support_files_copied:
        print(f"  Copied support files: {', '.join(support_files_copied)}")

    print("Parsing project into intermediate representation ...")
    ir = latex_parser.parse_latex_to_ir(main_tex_path)

    print("Copying referenced figures and bibliography files ...")
    copy_assets(ir, extract_dir, out_dir, ir["review_notes"])

    print(f"Rendering into target template: {target['name']} ...")
    tex_out = render_template.render(ir, target)
    main_out_name = "main.tex"
    (out_dir / main_out_name).write_text(tex_out, encoding="utf-8")

    checklist = render_template.build_checklist(ir, target)
    (out_dir / "REVIEW_CHECKLIST.md").write_text(checklist, encoding="utf-8")

    notes = build_conversion_notes(
        main_tex_path.relative_to(extract_dir), 
        [str(c.relative_to(extract_dir)) for c in all_candidates],
        ir, target)
    (out_dir / "CONVERSION_NOTES.txt").write_text(notes, encoding="utf-8")

    # Also drop the raw IR json in, for transparency / debugging if something looks off
    (out_dir / "_parsed_content.ir.json").write_text(json.dumps(ir, indent=2), encoding="utf-8")

    output_zip = Path(args.output)
    if output_zip.exists():
        output_zip.unlink()
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in out_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(out_dir))

    print(f"\nDone. Wrote {output_zip}")
    print(f"Contains: {main_out_name}, referenced figures/bib, REVIEW_CHECKLIST.md, "
          f"CONVERSION_NOTES.txt")
    if ir["review_notes"]:
        print("\nReview notes (also in REVIEW_CHECKLIST.md):")
        for n in ir["review_notes"]:
            print(f"  - {n}")


if __name__ == "__main__":
    sys.exit(main())
