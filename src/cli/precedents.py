"""Build precedent cards and fabula vectors from verdicts of the knowledge base.

Usage: python -m src.cli.precedents index
Idempotent: verdicts that already have a card and texts that already have a vector are skipped.
"""

import argparse
import asyncio
import json
from dataclasses import asdict

from src.application.precedent_cards import CARD_EXTRACTOR_VERSION
from src.application.similar_precedents import IndexPrecedents
from src.db.session import async_session_factory
from src.infrastructure.fastembed_embedder import FastembedEmbedder
from src.infrastructure.precedent_store import SqlPrecedentStore
from src.infrastructure.verdict_reader import PracticeReader


async def run_index(embedder=None) -> dict:
    async with async_session_factory() as db:
        use_case = IndexPrecedents(
            SqlPrecedentStore(db), PracticeReader(), embedder or FastembedEmbedder()
        )
        return asdict(await use_case.execute(CARD_EXTRACTOR_VERSION))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index", help="build missing cards and vectors")
    parser.parse_args()
    print(json.dumps(asyncio.run(run_index()), ensure_ascii=False))


if __name__ == "__main__":
    main()
