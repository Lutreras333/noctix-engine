"""Report: renders verified results into the documents humans read.

The renderer re-derives every citation against the documents it is
handed (its honesty gate) — a number it cannot re-derive is a number
it refuses to print.
"""

from noctix_engine.report.three_way_report import ReportError, render_three_way_report

__all__ = ["ReportError", "render_three_way_report"]
