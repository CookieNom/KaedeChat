"""Queue a complete, online-safe rebuild of the private message search index.

Chat remains available while this runs.  SQL is authoritative; the worker
drains the desired-state rows and Meilisearch can be empty or replaced.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, select

from app.core.settings import get_settings
from app.db.models import Message, SearchIndexState
from app.db.session import create_engine_and_sessionmaker
from app.search.meili import MeiliClient, SearchUnavailable


async def rebuild(*, reset_index: bool) -> int:
    settings = get_settings()
    if not settings.search_enabled or settings.search_master_key is None:
        raise RuntimeError(
            "message search is disabled; enable KAEDE_SEARCH_ENABLED and rerun preflight first"
        )
    if reset_index:
        await MeiliClient(settings).reset_index()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with sessionmaker() as session:
            count = int(await session.scalar(select(func.count()).select_from(Message)) or 0)
            state = await session.get(SearchIndexState, 1, with_for_update=True)
            if state is None:
                state = SearchIndexState(id=1)
                session.add(state)
            state.enabled = True
            state.reset_required = False
            state.backfill_after_id = None
            state.backfill_after_domain = None
            state.backfill_completed = False
            await session.commit()
            return count
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue every eligible Kaede message for private search reindexing."
    )
    parser.add_argument(
        "--reset-index",
        action="store_true",
        help="delete and recreate the Meilisearch index before rebuilding",
    )
    arguments = parser.parse_args()
    try:
        count = asyncio.run(rebuild(reset_index=arguments.reset_index))
    except SearchUnavailable as error:
        raise SystemExit(
            "Could not contact the private search service. Check the meilisearch service "
            "and KAEDE_SEARCH_URL, then retry."
        ) from error
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    print(f"Scheduled a rebuild of {count} messages. Indexing continues safely in the background.")


if __name__ == "__main__":
    main()
