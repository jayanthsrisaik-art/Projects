"""groundcheck: verify that each claim in an AI-written answer is backed by its sources."""

from .lexical import LexicalVerifier, LexicalConfig
from .report import Verdict, check_answer

__all__ = ["LexicalVerifier", "LexicalConfig", "Verdict", "check_answer"]
