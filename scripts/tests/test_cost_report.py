from datetime import date

import boto3
import pytest
from botocore.stub import Stubber

import cost_report as cr


def _group(key, amount):
    return {"Keys": [key], "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}}}


@pytest.fixture
def ce():
    client = boto3.client("ce", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    with Stubber(client) as stub:
        yield client, stub


def test_month_window_regular_day():
    assert cr.month_window(date(2026, 9, 28)) == (date(2026, 9, 1), date(2026, 9, 28))


def test_month_window_first_of_month_reports_previous_month():
    assert cr.month_window(date(2026, 10, 1)) == (date(2026, 9, 1), date(2026, 10, 1))


def test_previous_window_clamps_to_short_month():
    # 30 days of March compared against February (28 days in 2026)
    assert cr.previous_window(date(2026, 3, 1), date(2026, 3, 31)) == (date(2026, 2, 1), date(2026, 3, 1))


def test_build_report_aggregates_and_flags_budget(ce):
    client, stub = ce
    stub.add_response("get_cost_and_usage", {
        "ResultsByTime": [{"Groups": [_group("Amazon EKS", 1200.5), _group("Amazon RDS", 800)]}]},
        {"TimePeriod": {"Start": "2026-09-01", "End": "2026-09-28"}, "Granularity": "MONTHLY",
         "Metrics": ["UnblendedCost"], "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]})
    # paginated tag query: two pages
    stub.add_response("get_cost_and_usage", {
        "ResultsByTime": [{"Groups": [_group("CostCenter$CZ-1000", 1500)]}], "NextPageToken": "p2"}, None)
    stub.add_response("get_cost_and_usage", {
        "ResultsByTime": [{"Groups": [_group("CostCenter$", 500.5)]}]}, None)
    stub.add_response("get_cost_and_usage", {
        "ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "1600", "Unit": "USD"}}}]},
        {"TimePeriod": {"Start": "2026-08-01", "End": "2026-08-28"}, "Granularity": "MONTHLY",
         "Metrics": ["UnblendedCost"]})
    stub.add_response("get_cost_forecast", {"Total": {"Amount": "300", "Unit": "USD"}}, None)

    r = cr.build_report(client, date(2026, 9, 28), budget=2200)

    assert r.total_mtd == 2000.5
    assert list(r.by_service) == ["Amazon EKS", "Amazon RDS"]
    assert r.by_cost_center == {"CZ-1000": 1500.0, "(untagged)": 500.5}
    assert r.untagged_spend == 500.5
    assert r.previous_month_same_period == 1600.0
    assert r.mom_change_pct == 25.03
    assert r.forecast_month_end == 2300.5
    assert r.over_budget is True
    stub.assert_no_pending_responses()


def test_forecast_unavailable_is_tolerated(ce):
    client, stub = ce
    stub.add_client_error("get_cost_forecast", service_error_code="DataUnavailableException")
    assert cr._forecast(client, date(2026, 9, 15)) is None


def test_renderers():
    r = cr.CostReport("2026-09-01", "2026-09-28", total_mtd=10, by_service={"EC2": 10},
                      by_cost_center={"CZ-1000": 10})
    assert "EC2" in cr.render_table(r)
    csv_text = cr.render_csv(r)
    assert csv_text.splitlines()[0] == "dimension,key,amount,currency"
    assert "cost_center,CZ-1000,10,USD" in csv_text
    assert r.to_dict()["over_budget"] is False
