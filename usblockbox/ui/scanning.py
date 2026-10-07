"""Text for the empty tile area: "Scanning..." while the first scan runs, then "No USB ports found"."""
from __future__ import annotations

SLOW_AFTER = 15      # seconds before the text says it is still working
VERY_SLOW_AFTER = 45  # seconds before it says this PC is slow and where the log is


def scanning_text(elapsed: float, done: bool, error: str = "", note: str = "") -> str:
    """What to show while there are no port tiles. `elapsed` is seconds since the scan began."""
    if error:
        return ("Could not scan the USB ports yet. Trying again...\n" + error[:200] +
                "\nDetails are in the Data\\logs folder (Diagnostics > Open logs folder).")
    if done:
        return "No USB ports found.\n" + (note or "Plug in a USB hub, or check Settings > Ports.")
    dots = "." * (int(elapsed) % 3 + 1)
    text = "Scanning USB ports" + dots
    if elapsed < SLOW_AFTER:
        return text
    text += "\nStill working (%d s)." % int(elapsed)
    if elapsed >= VERY_SLOW_AFTER:
        text += " This PC is slow to answer; details are being written to Data\\logs."
    return text
