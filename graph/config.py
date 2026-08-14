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
}

# edge decay half-lives in days by predicate keyword (design §9); None = structural
HALF_LIFE_RULES = [
    ({"own", "subsidiary", "parent", "incorporat", "headquarter", "listed",
      "founded", "ceo", "cfo", "chair", "director", "appointed", "serves"}, None),
    ({"acquir", "merg", "partner", "deal", "agreement", "contract", "invest",
      "backs", "funding"}, 180),
    ({"suppl", "customer", "depends", "sources"}, 540),
    ({"litigat", "sued", "lawsuit"}, 365),
]
HALF_LIFE_DEFAULT = 365
