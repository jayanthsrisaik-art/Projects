"""semguard: a guarded semantic cache for LLM calls."""
from .cache import CacheConfig, Lookup, SemanticCache, scope_key
from .embed import HashingEmbedder
from .facts import Lexicon, extract_facts, conflict

__all__ = ["CacheConfig", "Lookup", "SemanticCache", "scope_key", "HashingEmbedder",
           "Lexicon", "extract_facts", "conflict"]
