"""Pure scoring function. No stored totals."""


def score(entries):
    """entries: iterable of dicts with 'scale', 'direction', 'status'.
    Only entries with status == 'approved' count."""
    pos = neg = 0.0
    for e in entries:
        if e.get("status") != "approved":
            continue
        if e["direction"] == 1:
            pos += float(e["scale"])
        elif e["direction"] == -1:
            neg += float(e["scale"])
    return {"positive_total": pos, "negative_total": neg, "net": pos - neg}
