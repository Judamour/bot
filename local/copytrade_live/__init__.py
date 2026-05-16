"""Polymarket CopyTrade Live — local execution layer.

Architecture:
  poller.py  — main loop: ssh-poll VPS decisions.jsonl, dispatch BUY/SELL
  executor.py — py-clob-client wrapper, signs and submits orders
  state.py   — atomic persistence: last_seen_ts, positions, equity
  config.py  — env loading, constants

Runs from the user's local machine connected to NordVPN Canada.
Polymarket private key stays local — never on the VPS.
"""
