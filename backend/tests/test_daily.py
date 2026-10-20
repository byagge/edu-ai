"""Daily jobs the education vertical arms on top of the shared worker."""
from __future__ import annotations

from sqlalchemy import select

from arix.core.context import load_context
from arix.db.models_core import ScheduledJob
from arix.db.session import tenant_session
from arix.verticals.edu.handlers import arm_daily

from .conftest import requires_db

pytestmark = requires_db


async def test_daily_arm_enqueues_rollout_and_announce_only(tenant):
    """20 October is neither the 1st nor 1 August / 1 December.

    Seasonal reactivation and the monthly parent report must stay unarmed.
    """
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        await arm_daily(session, ctx)
        kinds = set(
            (
                await session.execute(
                    select(ScheduledJob.kind).where(ScheduledJob.tenant_id == tenant)
                )
            ).scalars()
        )
    assert "lessons_rollout" in kinds
    assert "group_start_announce" in kinds
    assert "reactivation" not in kinds
    assert "parent_report" not in kinds
