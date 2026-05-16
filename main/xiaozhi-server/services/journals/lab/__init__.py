"""Journal lab tooling.

This package is for counterfactual journal replay experiments over beta history.
It is intentionally separate from production journal jobs. Lab commands may read
from a beta-history Supabase source using JOURNAL_LAB_SOURCE_* env vars, but the
initial lab tooling does not write to Supabase, Firestore, or production data.
"""
