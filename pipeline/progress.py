"""Tiny progress bus. CLI just prints; the web runner installs a hook to update the UI."""
_hook = None


def set_hook(fn):
    global _hook
    _hook = fn


def report(msg: str = None, pct: float = None, eta: float = None, quiet: bool = False):
    """msg: human message; pct: 0..1 progress of the current step; eta: seconds left in the step."""
    if msg and not quiet:
        print(msg, flush=True)
    if _hook:
        try:
            _hook(msg=msg, pct=pct, eta=eta)
        except Exception:
            pass
