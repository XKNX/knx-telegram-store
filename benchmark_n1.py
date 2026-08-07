import asyncio
import time
from sqlalchemy import Table, Column, Integer, String, Text, MetaData, tuple_, select, insert
from sqlalchemy.ext.asyncio import create_async_engine

metadata = MetaData()
table = Table(
    "string_lookup",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("category", String(30), nullable=False),
    Column("value", Text, nullable=False),
)

async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # insert 1000 items
        items = [{"category": f"cat_{i}", "value": f"val_{i}"} for i in range(1000)]
        await conn.execute(insert(table), items)

    to_resolve = [(f"cat_{i}", f"val_{i}") for i in range(1000)]

    async with engine.connect() as conn:
        # N+1 test
        start = time.perf_counter()
        for cat, val in to_resolve:
            row_id = await conn.scalar(select(table.c.id).where(table.c.category == cat, table.c.value == val))
        end = time.perf_counter()
        n1_time = end - start
        print(f"N+1 approach took: {n1_time:.4f}s")

        # IN test
        start = time.perf_counter()
        stmt = select(table.c.category, table.c.value, table.c.id).where(
            tuple_(table.c.category, table.c.value).in_(to_resolve)
        )
        result = await conn.execute(stmt)
        rows = result.all()
        end = time.perf_counter()
        in_time = end - start
        print(f"IN approach took: {in_time:.4f}s")
        print(f"Improvement: {n1_time / in_time:.2f}x")

asyncio.run(main())
