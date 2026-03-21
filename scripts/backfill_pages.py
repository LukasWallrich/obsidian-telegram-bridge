#!/usr/bin/env python3
"""Backfill: render all existing vault notes to GitHub Pages."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))

from config import settings
from pages import backfill_all, git_commit_and_push

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

if not settings.pages_repo_path or not settings.pages_base_url:
    print("Error: PAGES_REPO_PATH and PAGES_BASE_URL must be set in .env")
    sys.exit(1)

count = backfill_all(
    settings.obsidian_vault_path,
    settings.obsidian_resource_folder,
    settings.pages_repo_path,
    settings.pages_base_url,
)

if count > 0:
    success = git_commit_and_push(settings.pages_repo_path, f"Backfill: {count} notes")
    if success:
        print(f"Published {count} notes and pushed to GitHub Pages")
    else:
        print(f"Rendered {count} notes but git push failed — try pushing manually")
else:
    print("No notes found to backfill")
