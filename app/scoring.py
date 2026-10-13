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


def by_category(entries, include_estimates=True):
    """{(category, unit): score(...)} over each entry's category/unit (default people/people)."""
    groups = {}
    for e in entries:
        key = (e.get("category") or "people", e.get("unit") or "people")
        groups.setdefault(key, []).append(e)
    return {k: score(v, include_estimates) for k, v in groups.items()}


def overall(entries, rates, include_estimates=True):
    """Common-unit (USD) score using only officially published rates {(category, unit): usd_per_unit}.
    Categories without a rate are listed in not_convertible and excluded."""
    pos = neg = 0.0
    not_conv = []
    for key, sc in by_category(entries, include_estimates).items():
        if key not in rates:
            if sc["positive_total"] or sc["negative_total"]:
                not_conv.append(key)
            continue
        pos += sc["positive_total"] * rates[key]
        neg += sc["negative_total"] * rates[key]
    total = pos + neg
    return {"overall": pos - neg, "overall_positive": pos, "overall_negative": neg,
            "positive_pct": pos / total * 100 if total else None,
            "negative_pct": neg / total * 100 if total else None,
            "not_convertible": sorted(not_conv)}
