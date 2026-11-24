#!/usr/bin/env python3
"""
UserPromptSubmit hook that assigns a random memorable name to each terminal session.

Generates a two-word name (adjective-animal) on first prompt, stores it in
.claude/terminal-identity.local.json. Prints a small tag on EVERY prompt
so the user can always see which terminal they're in without scrolling.
"""
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

ADJECTIVES = [
    "cosmic", "thunder", "violet", "neon", "shadow", "crystal", "phantom",
    "golden", "iron", "arctic", "blazing", "crimson", "mystic", "quantum",
    "swift", "silent", "brave", "clever", "fierce", "noble", "lunar",
    "solar", "frosty", "ember", "coral", "azure", "scarlet", "jade",
    "obsidian", "silver", "copper", "ruby", "amber", "ivory", "onyx",
    "turbo", "rapid", "primal", "vivid", "bold", "keen", "wild",
    "stormy", "dusty", "pixel", "cyber", "stealth", "sonic", "atomic",
    "radiant",
]

ANIMALS = [
    "penguin", "falcon", "panther", "wolf", "dragon", "phoenix", "tiger",
    "cobra", "raven", "hawk", "fox", "bear", "shark", "eagle", "lynx",
    "otter", "viper", "mustang", "jaguar", "puma", "dolphin", "mantis",
    "condor", "badger", "bison", "crane", "gecko", "heron", "ibex",
    "koala", "lemur", "moose", "narwhal", "osprey", "parrot", "quail",
    "raptor", "salmon", "toucan", "urchin", "walrus", "yak", "zebra",
    "coyote", "ferret", "gorilla", "hyena", "iguana", "jackal", "octopus",
]


def main() -> int:
    project_dir = os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd()
    identity_path = Path(project_dir) / ".claude" / "terminal-identity.local.json"

    name = None

    # Try to read existing identity
    if identity_path.exists():
        try:
            data = json.loads(identity_path.read_text(encoding="utf-8"))
            name = data.get("name")
        except (json.JSONDecodeError, OSError):
            pass

    # Generate new name if none exists
    if not name:
        name = f"{random.choice(ADJECTIVES)}-{random.choice(ANIMALS)}"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(
            json.dumps(
                {
                    "session_id": os.getenv("CLAUDE_SESSION_ID", ""),
                    "name": name,
                    "created_at": datetime.utcnow().isoformat() + "Z",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # Print tag on EVERY prompt so user always knows which terminal this is
    sys.stderr.write(f"[{name}]\n")
    sys.stderr.flush()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Never block — always exit 0
        sys.exit(0)
