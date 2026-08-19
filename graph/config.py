"""Environment-driven configuration. Everything has a local-dev default."""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DB_URL = os.environ.get("CAF_DB_URL", "postgresql:///caf_graph")
ARTIFACTS = Path(os.environ.get("CAF_ARTIFACTS", str(REPO / "var" / "artifacts")))
GLEIF_SQLITE = Path(os.environ.get("CAF_GLEIF", str(REPO / "spike" / "corpus" / "ref" / "gleif.sqlite")))
SEC_TICKERS = REPO / "spike" / "corpus" / "ref" / "company_tickers.json"
ALIASES = REPO / "spike" / "corpus" / "ref" / "aliases.json"
WATCHLIST = REPO / "spike" / "corpus" / "ref" / "watchlist.json"
PROMPTS = REPO / "graph" / "prompts"

USER_AGENT = "CAF-Vault research moptclaude@gmail.com"

# model tiers (design §11): triage cheap, extraction workhorse, strong tier later
MODELS = {
    "triage": os.environ.get("CAF_MODEL_TRIAGE", "claude-haiku-4-5-20251001"),
    "extract": os.environ.get("CAF_MODEL_EXTRACT", "claude-sonnet-5"),
    "adjudicate": os.environ.get("CAF_MODEL_ADJUDICATE", "claude-sonnet-5"),
    "hypothesize": os.environ.get("CAF_MODEL_HYPOTHESIZE", "claude-sonnet-5"),
    "investigate": os.environ.get("CAF_MODEL_INVESTIGATE", "claude-sonnet-5"),
    "verify": os.environ.get("CAF_MODEL_VERIFY", "claude-sonnet-5"),
    "garden": os.environ.get("CAF_MODEL_GARDEN", "claude-sonnet-5"),
    "summarize": os.environ.get("CAF_MODEL_SUMMARIZE", "claude-sonnet-5"),
}

# document summaries (build spec v5): per-cycle cap on the worker's summarize
# stage, the body length under which a document is not worth a model call,
# and the prompt cap (extraction uses 60k for the same reason)
SUMMARIZE_PER_CYCLE = int(os.environ.get("CAF_VAULT_SUMMARIZE_PER_CYCLE", "30"))
SUMMARY_MIN_CHARS = 600
SUMMARY_MAX_CHARS = 80000

# edge decay half-lives in days by predicate keyword (design §9); None = structural
HALF_LIFE_RULES = [
    ({"own", "subsidiary", "parent", "incorporat", "headquarter", "listed",
      "founded", "ceo", "cfo", "chair", "director", "appointed", "serves"}, None),
    ({"acquir", "merg", "partner", "deal", "agreement", "contract", "invest",
      "backs", "funding"}, 180),
    ({"suppl", "customer", "depend", "sources"}, 540),
    ({"litigat", "sued", "lawsuit"}, 365),
]
HALF_LIFE_DEFAULT = 365

# claim confidence staleness (design §10.2): per-predicate half-life in days,
# None = does not decay. Distinct from edge relevance (§9).
CLAIM_HALF_LIFE_RULES = [
    ({"headcount", "guidance", "price", "forecast", "expects"}, 90),
    ({"revenue", "profit", "earnings", "margin", "reported"}, 365),
    ({"incorporat", "founded", "headquarter", "own", "subsidiary", "listed"}, None),
]
CLAIM_HALF_LIFE_DEFAULT = 540

# discovery funnel (design §8): per-cycle LLM caps and scoring knobs
DISCOVERY = {
    "triage_top_k": int(os.environ.get("CAF_VAULT_HYP_TRIAGE_K", "5")),
    "hypothesize_per_cycle": int(os.environ.get("CAF_VAULT_HYP_PER_CYCLE", "3")),
    "investigate_per_cycle": int(os.environ.get("CAF_VAULT_INV_PER_CYCLE", "2")),
    "verify_per_cycle": int(os.environ.get("CAF_VAULT_VER_PER_CYCLE", "2")),
    "budget_tool_calls": 8,        # hard cap per investigation (§8.4)
    "budget_tokens": 60000,        # approximate prompt+response ceiling
    "strategy_prior": {"attribute_join_v0": 0.6},
    "min_score": 0.05,             # below this a candidate is never triaged
}
# independent lineages required to promote, by hypothesis type (§8.5)
LINEAGES_REQUIRED = {"shared_dependency": 2}
LINEAGES_REQUIRED_DEFAULT = 2

# fast-path materiality threshold for alerts (design §3)
ALERT_MATERIALITY_MIN = 0.6
