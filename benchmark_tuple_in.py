import asyncio
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
        await conn.execute(insert(table).values(category="src", value="1.1.1"))
        await conn.execute(insert(table).values(category="dst", value="1/1/1"))

    async with engine.connect() as conn:
        to_resolve = [("src", "1.1.1"), ("dst", "1/1/1")]
        stmt = select(table.c.category, table.c.value, table.c.id).where(
            tuple_(table.c.category, table.c.value).in_(to_resolve)
        )
        result = await conn.execute(stmt)
        for row in result:
            print(row)

asyncio.run(main())
