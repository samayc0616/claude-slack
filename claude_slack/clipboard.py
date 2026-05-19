"""OSC 52 clipboard copy. Works over SSH and inside tmux/screen
(modern tmux needs `set -g allow-passthrough on` and `set -g set-clipboard on`)."""
from __future__ import annotations

import base64
import os
import sys


def copy(text: str) -> bool:
    """Emit OSC 52 to set the system clipboard. Returns True if the sequence was
    written (we cannot actually verify the terminal honored it)."""
    try:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        seq = f"\x1b]52;c;{b64}\x07"

        # tmux: wrap each ESC so the inner sequence passes through to the outer terminal.
        if os.environ.get("TMUX"):
            seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
        # GNU screen: similar pass-through wrap.
        elif (os.environ.get("TERM") or "").startswith("screen"):
            seq = "\x1bP" + seq + "\x1b\\"

        sys.stdout.write(seq)
        sys.stdout.flush()
        return True
    except Exception:
        return False
