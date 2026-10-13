# Verification of complete corrected EW/IW processing

Real data execution, 13 October 2026: eighteen new pinned SNAP14 corrected-power references, complete source measurement scans, complete corrected four-band exports, lossless BigTIFF replacement packaging, and the final source-bound comparison all completed. Both representative products pass the unchanged source and reference thresholds. The final report explicitly leaves fresh target detection, regional deployment and scientific release closed.

Every original and replacement base pixel was compared; corrected measurements, metadata, grid and nodata are identical. All stored overview pixels were decoded in bounded blocks and checked. The original EW conversion logged classic-TIFF temporary-size errors; that history remains visible in the original processing log. Explicit BigTIFF packaging resolves the format path without changing physical samples or weakening limits.

Local portable suite: **605 passed, 14 skipped**, with 79 warnings, mostly fixture georeferencing and existing dependency warnings. Fourteen real-imagery tests require the archive outside this worktree. Ruff passes; formatting checks 193 files. Mypy passes all 155 maintained source/test/validation files. The native delivery pipeline will separately review the change, run its checks, push a stacked PR and verify CI.

Behavioral regressions exercise tile-boundary invariance, source/record preservation, strict pre-export rejection, common nodata and ratio relationships, source changes during execution, source-window bounds, planted radiometric and spatial errors, mismatched reference grids, BigTIFF generation and pixel identity, and installed-package execution provenance without Git. Tests do not infer behavior by searching implementation strings.

The execution snapshots bind the actual generating code and dirty parent checkout. After scientific execution, a typing-only guard and current prose/config comments were corrected; the immutable reports and snapshots were not rewritten to represent a later commit. No held-out detector images, charts, pilot submissions or human label records were changed.
