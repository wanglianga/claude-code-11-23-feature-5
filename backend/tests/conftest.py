import os

os.environ["IRR_DATABASE_URL"] = (
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/irrigation_test")

import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def clean_db():
    """清库但保留表结构，供脚本级测试使用。"""
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE adjustment_candidates, attribution_conflicts, "
            "difference_bridge_entries, receipt_matches, billing_snapshot_items, "
            "billing_snapshots, billing_fragments, external_billing_entries, "
            "price_relief_rules, price_tiers, price_versions, tenure_records, "
            "billing_operators, metric_versions, recompute_runs, "
            "reconciliation_tasks, adjudications, quality_events, supply_facts, "
            "meter_changes, plot_batch_allocations, device_mappings, "
            "mapping_versions, rotation_batches, plots, meter_points, channels, "
            "manual_revisions, raw_messages, data_contracts RESTART IDENTITY CASCADE"))
    yield
