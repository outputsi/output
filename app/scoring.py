"""Pure scoring function. No stored totals."""


def effective_scale(e):
    """scale * credit_share (credit_share missing/None -> 1.0)."""
    share = e.get("credit_share")
    return float(e["scale"]) * (1.0 if share is None else float(share))


def score(entries, include_estimates=False):
    """entries: iterable of dicts with 'scale', 'direction', 'status'.
    Only entries with status == 'approved' count. Each contributes its effective scale
    (scale * credit_share). Entries with is_estimate truthy are skipped unless include_estimates.
    positive_pct = positive_total / (positive_total + negative_total) * 100, None when both are 0.
    negative_pct = negative_total / (positive_total + negative_total) * 100, None when both are 0."""
    pos = neg = 0.0
    for e in entries:
        if e.get("status") != "approved":
            continue
        if e.get("is_estimate") and not include_estimates:
            continue
        if e["direction"] == 1:
            pos += effective_scale(e)
        elif e["direction"] == -1:
            neg += effective_scale(e)
    total = pos + neg
    pct = pos / total * 100 if total else None
    npct = neg / total * 100 if total else None
    return {"positive_total": pos, "negative_total": neg, "net": pos - neg,
            "positive_pct": pct, "negative_pct": npct}


def fmt_pct(pct):
    """Display helper: '100%', '90%', '33.3%', or an em dash when undefined."""
    if pct is None:
        return "\u2014"
    return f"{pct:.1f}".rstrip("0").rstrip(".") + "%"


def credit_label(e):
    """'Direct', 'Sourced share 60%', 'Even split 1/4'."""
    basis = e.get("credit_basis") or "direct"
    share = e.get("credit_share")
    share = 1.0 if share is None else float(share)
    if basis == "sourced_share":
        return "Sourced share " + fmt_pct(share * 100)
    if basis == "organization":
        return "Organization" if share == 1 else "Organization " + fmt_pct(share * 100)
    if basis == "even_split":
        return f"Even split 1/{round(1 / share)}" if share > 0 else "Even split"
    return "Direct" if share == 1 else "Direct " + fmt_pct(share * 100)
