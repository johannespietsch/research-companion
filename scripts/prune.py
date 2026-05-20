"""Delete rows nothing needs to keep, to bound disk growth on the Fly volume.

Run periodically (e.g. a daily Fly scheduled machine or cron):

    python -m scripts.prune

Prunes old error_log rows, expired url_cache entries, and expired link codes.
Safe to run repeatedly. See bot.db.prune_maintenance for the policy.
"""
from __future__ import annotations

import logging

from bot.db import prune_maintenance

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    counts = prune_maintenance()
    logger.info(
        "prune: error_log=%(error_log)s url_cache=%(url_cache)s link_codes=%(link_codes)s",
        counts,
    )


if __name__ == "__main__":
    main()
