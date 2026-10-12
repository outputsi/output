"""Pure scoring function. No stored totals."""


def score(entries):
    """entries: iterable of dicts with 'scale', 'direction', 'status'.
    Only entries with status == 'approved' count.
    positive_pct = positive_total / (positive_total + negative_total) * 100, None when both are 0."""
    pos = neg = 0.0
    for e in entries:
        if e.get("status") != "approved":
            continue
        if e["direction"] == 1:
            pos += float(e["scale"])
        elif e["direction"] == -1:
            neg += float(e["scale"])
    total = pos + neg
    pct = pos / total * 100 if total else None
    return {"positive_total": pos, "negative_total": neg, "net": pos - neg, "positive_pct": pct}


def fmt_pct(pct):
    """Display helper: '100%', '90%', '33.3%', or an em dash when undefined."""
    if pct is None:
        return "\u2014"
    return f"{pct:.1f}".rstrip("0").rstrip(".") + "%"
