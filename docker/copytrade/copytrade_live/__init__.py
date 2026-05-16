"""Polymarket CopyTrade Live — VPS Docker variant.

Same logic as local/copytrade_live but reads decisions.jsonl directly from
the mounted volume (no SSH) since the container runs on the VPS itself.
"""
