"""In-app alerts (build spec v3 §4). Pipeline stages call emit(); the API
reads. No external channels — owner direction, 2026-08-17."""


def emit(con, kind, title, body=None, entity_ids=(), event_id=None,
         hypothesis_id=None):
    """Insert one alert. Re-emits for the same (kind, event) or
    (kind, hypothesis) are dropped by the partial unique indexes."""
    con.execute(
        "insert into alert (kind, title, body, entity_ids, event_id, "
        "hypothesis_id) values (%s, %s, %s, %s, %s, %s) "
        "on conflict do nothing",
        (kind, title, body, list(entity_ids), event_id, hypothesis_id))
