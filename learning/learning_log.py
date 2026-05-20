"""
learning/learning_log.py
------------------------
LearningLog — placeholder until full implementation in Phase 5 File 4.
"""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from agent.profile import OrgProfile


class LearningLog:
    def __init__(self, profile: OrgProfile) -> None:
        self.profile  = profile
        self.log_dir  = Path("outputs") / self._org_slug() / "learning_log"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_submission(self, submission: dict, learned: str, changes_made: list) -> None:
        entry = {
            "timestamp":    datetime.now().isoformat(),
            "submission":   submission,
            "learned":      learned,
            "changes_made": changes_made,
        }
        log_file = self.log_dir / f"entry_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(log_file, "w") as f:
            json.dump(entry, f, indent=2)
        print(f"[LearningLog] Entry saved: {log_file}")

    def _org_slug(self) -> str:
        return self.profile.org_short_name.lower().replace(" ", "_").replace("'", "")