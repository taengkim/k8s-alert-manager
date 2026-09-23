"""Regression test for app.db.register_sqlite_pragmas: FK enforcement must
actually be on for the app's own engine construction path (app/db.py,
app/worker/runner.py), not just the test fixture's separately hand-rolled
one in conftest.py -- otherwise `ON DELETE CASCADE` never fires.

That matters more than a typical "did we forget a pragma" bug: SQLite
reuses a table's rowid after a row is deleted (none of these tables use
AUTOINCREMENT), so an orphaned `routing_matchers`/`routing_rule_channels`
row left behind by a rule delete can silently reattach itself to a later,
unrelated rule that happens to get the same id -- a routing rule could
start matching/notifying through matchers and channels it was never
configured with. This test builds a fresh on-disk (not :memory:) engine the
same way app/db.py itself does and proves the cascade actually happens.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, register_sqlite_pragmas
from app.models.channel import Channel
from app.models.routing import RoutingMatcher, RoutingRule, routing_rule_channels
from app.models.team import Team


async def test_app_style_engine_enforces_fk_cascade_on_delete(tmp_path) -> None:
    db_path = tmp_path / "pragma-regression.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    register_sqlite_pragmas(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            # Sanity: the pragma is actually in effect on this connection.
            raw = await session.connection()
            result = await raw.exec_driver_sql("PRAGMA foreign_keys")
            assert result.scalar() == 1

            team = Team(slug="pragma-team", name="Pragma Team")
            session.add(team)
            await session.flush()

            channel = Channel(
                team_id=team.id, name="c1", type="email", config_encrypted="unused"
            )
            session.add(channel)
            await session.flush()

            rule = RoutingRule(
                team_id=team.id,
                name="r1",
                action="notify",
                channels=[channel],
                matchers=[
                    RoutingMatcher(kind="include", target="alertname", pattern="x", position=0)
                ],
            )
            session.add(rule)
            await session.commit()
            rule_id = rule.id

            await session.delete(rule)
            await session.commit()

            orphan_matchers = (
                await session.execute(
                    select(RoutingMatcher).where(RoutingMatcher.routing_rule_id == rule_id)
                )
            ).scalars().all()
            assert orphan_matchers == []

            orphan_joins = (
                await session.execute(
                    select(routing_rule_channels).where(
                        routing_rule_channels.c.routing_rule_id == rule_id
                    )
                )
            ).all()
            assert orphan_joins == []

            # Rowid-reuse check: a brand new rule inserted after the delete
            # can legitimately get the same id back from SQLite -- if the
            # cascade above hadn't actually run, this new rule would come
            # back with the old rule's matcher/channel already attached.
            new_rule = RoutingRule(team_id=team.id, name="r2", action="notify")
            session.add(new_rule)
            await session.commit()
            await session.refresh(new_rule, attribute_names=["matchers", "channels"])
            assert new_rule.matchers == []
            assert new_rule.channels == []
    finally:
        await engine.dispose()
