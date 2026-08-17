"""Claim confidence staleness (design §10.2): a claim's usable confidence
decays with age by predicate type. Query-time only — nothing is written.
Distinct from edge relevance (§9), which decays links, not claims."""
from datetime import datetime, timezone

from . import config


def half_life_for(predicate: str):
    """First CLAIM_HALF_LIFE_RULES entry with a keyword contained in the
    predicate wins; None means the claim does not decay."""
    p = (predicate or "").lower()
    for keywords, days in config.CLAIM_HALF_LIFE_RULES:
        if any(k in p for k in keywords):
            return days
    return config.CLAIM_HALF_LIFE_DEFAULT


def confidence_now(confidence, observed_at, predicate, at=None):
    """confidence * 2^(-age_days / half_life), 2dp. Returns the input
    confidence unchanged when it or observed_at is missing."""
    if confidence is None or observed_at is None:
        return confidence
    days = half_life_for(predicate)
    if days is None:
        return round(float(confidence), 2)
    if at is None:
        at = datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (at - observed_at).total_seconds() / 86400.0)
    return round(float(confidence) * 2 ** (-age_days / days), 2)
