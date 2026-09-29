#!/usr/bin/env python3
"""
CzarIntel FinOps cost report (AWS Cost Explorer via boto3).

Produces month-to-date spend grouped by service and by CostCenter tag, a
forecast for the full month, a comparison with the previous month and a list
of untagged spend (a SOC2 CC1.3 / FinOps accountability finding).

Authentication uses the ambient AWS credential chain (SSO / OIDC web identity
in CI) - no static keys.

    python3 scripts/cost_report.py                        # table to stdout
    python3 scripts/cost_report.py --format json -o reports/cost.json
    python3 scripts/cost_report.py --budget 25000 --fail-over-budget   # CI gate
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger("cost_report")
METRIC = "UnblendedCost"


@dataclass
class CostReport:
    period_start: str
    period_end: str
    currency: str = "USD"
    total_mtd: float = 0.0
    previous_month_same_period: float = 0.0
    forecast_month_end: float | None = None
    by_service: dict[str, float] = field(default_factory=dict)
    by_cost_center: dict[str, float] = field(default_factory=dict)
    untagged_spend: float = 0.0
    budget: float | None = None

    @property
    def mom_change_pct(self) -> float | None:
        if not self.previous_month_same_period:
            return None
        return round((self.total_mtd - self.previous_month_same_period) / self.previous_month_same_period * 100, 2)

    @property
    def over_budget(self) -> bool:
        if self.budget is None:
            return False
        projected = self.forecast_month_end if self.forecast_month_end is not None else self.total_mtd
        return projected > self.budget

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mom_change_pct"] = self.mom_change_pct
        d["over_budget"] = self.over_budget
        return d


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def month_window(today: date) -> tuple[date, date]:
    """Return [first-of-month, today) - Cost Explorer's End is exclusive.

    On the 1st of the month there is no MTD data yet, so report the previous month.
    """
    start = today.replace(day=1)
    if start == today:
        prev_end = start
        start = (start - timedelta(days=1)).replace(day=1)
        return start, prev_end
    return start, today


def previous_window(start: date, end: date) -> tuple[date, date]:
    """Same number of days in the previous month (clamped to that month's length)."""
    prev_start = (start - timedelta(days=1)).replace(day=1)
    days = (end - start).days
    prev_month_len = (start - prev_start).days
    return prev_start, prev_start + timedelta(days=min(days, prev_month_len))


def month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt


# --------------------------------------------------------------------------- #
# Cost Explorer queries
# --------------------------------------------------------------------------- #
def _sum_groups(results: list[dict], key_fn=lambda k: k) -> tuple[dict[str, float], str]:
    out: dict[str, float] = {}
    currency = "USD"
    for period in results:
        for g in period.get("Groups", []):
            k = key_fn(g["Keys"][0])
            amt = g["Metrics"][METRIC]
            currency = amt.get("Unit", currency)
            out[k] = out.get(k, 0.0) + float(amt["Amount"])
    return out, currency


def _paginate(ce, **kwargs) -> list[dict]:
    results, token = [], None
    while True:
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        results.extend(resp.get("ResultsByTime", []))
        token = resp.get("NextPageToken")
        if not token:
            return results


def _cost_by(ce, start: date, end: date, group: dict) -> tuple[dict[str, float], str]:
    results = _paginate(
        ce,
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=[METRIC],
        GroupBy=[group],
    )
    if group["Type"] == "TAG":
        # Tag keys come back as "CostCenter$CZ-1000"; empty value == untagged
        return _sum_groups(results, lambda k: k.split("$", 1)[1] or "(untagged)")
    return _sum_groups(results)


def _total(ce, start: date, end: date) -> float:
    results = _paginate(
        ce,
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=[METRIC],
    )
    return sum(float(r["Total"][METRIC]["Amount"]) for r in results)


def _forecast(ce, today: date) -> float | None:
    start, end = today, month_end(today)
    if (end - start).days < 1:
        return None
    try:
        resp = ce.get_cost_forecast(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
        return float(resp["Total"]["Amount"])
    except ClientError as exc:
        # DataUnavailableException is normal for new accounts
        LOG.warning("forecast unavailable: %s", exc.response.get("Error", {}).get("Code"))
        return None


def build_report(ce, today: date, tag_key: str = "CostCenter", budget: float | None = None) -> CostReport:
    start, end = month_window(today)
    by_service, currency = _cost_by(ce, start, end, {"Type": "DIMENSION", "Key": "SERVICE"})
    by_cc, _ = _cost_by(ce, start, end, {"Type": "TAG", "Key": tag_key})
    p_start, p_end = previous_window(start, end)

    report = CostReport(
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        currency=currency,
        by_service={k: round(v, 2) for k, v in sorted(by_service.items(), key=lambda kv: -kv[1])},
        by_cost_center={k: round(v, 2) for k, v in sorted(by_cc.items(), key=lambda kv: -kv[1])},
        budget=budget,
    )
    report.total_mtd = round(sum(by_service.values()), 2)
    report.untagged_spend = round(by_cc.get("(untagged)", 0.0), 2)
    report.previous_month_same_period = round(_total(ce, p_start, p_end), 2)
    if end > start and today.day != 1:
        fc = _forecast(ce, today)
        report.forecast_month_end = round(report.total_mtd + fc, 2) if fc is not None else None
    return report


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_table(r: CostReport, top: int = 10) -> str:
    buf = io.StringIO()
    w = buf.write
    w(f"CzarIntel AWS cost report  {r.period_start} -> {r.period_end} ({r.currency})\n")
    w("=" * 64 + "\n")
    w(f"{'Month-to-date':<36}{r.total_mtd:>14,.2f}\n")
    w(f"{'Previous month (same days)':<36}{r.previous_month_same_period:>14,.2f}\n")
    if r.mom_change_pct is not None:
        w(f"{'Change':<36}{r.mom_change_pct:>13.1f}%\n")
    if r.forecast_month_end is not None:
        w(f"{'Forecast (month end)':<36}{r.forecast_month_end:>14,.2f}\n")
    if r.budget is not None:
        w(f"{'Budget':<36}{r.budget:>14,.2f}  {'OVER' if r.over_budget else 'ok'}\n")
    w(f"{'Untagged spend':<36}{r.untagged_spend:>14,.2f}\n\n")
    w(f"Top {top} services\n" + "-" * 64 + "\n")
    for k, v in list(r.by_service.items())[:top]:
        w(f"{k[:48]:<50}{v:>14,.2f}\n")
    w("\nBy cost center\n" + "-" * 64 + "\n")
    for k, v in r.by_cost_center.items():
        w(f"{k[:48]:<50}{v:>14,.2f}\n")
    return buf.getvalue()


def render_csv(r: CostReport) -> str:
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["dimension", "key", "amount", "currency"])
    for k, v in r.by_service.items():
        wr.writerow(["service", k, v, r.currency])
    for k, v in r.by_cost_center.items():
        wr.writerow(["cost_center", k, v, r.currency])
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CzarIntel AWS cost report")
    ap.add_argument("--format", choices=["table", "json", "csv"], default="table")
    ap.add_argument("-o", "--output", help="write to file instead of stdout")
    ap.add_argument("--tag-key", default="CostCenter")
    ap.add_argument("--budget", type=float, help="monthly budget in report currency")
    ap.add_argument("--fail-over-budget", action="store_true", help="exit 2 if forecast exceeds budget")
    ap.add_argument("--profile", help="AWS profile (defaults to ambient credentials)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    ce = session.client("ce", region_name="us-east-1")  # Cost Explorer is global, served from us-east-1
    try:
        report = build_report(ce, date.today(), args.tag_key, args.budget)
    except (ClientError, BotoCoreError) as exc:
        LOG.error("Cost Explorer query failed: %s", exc)
        return 1

    text = {"table": lambda: render_table(report),
            "json": lambda: json.dumps(report.to_dict(), indent=2) + "\n",
            "csv": lambda: render_csv(report)}[args.format]()
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        LOG.info("wrote %s", args.output)
    else:
        sys.stdout.write(text)

    if args.fail_over_budget and report.over_budget:
        LOG.error("projected spend exceeds budget %.2f", report.budget)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
