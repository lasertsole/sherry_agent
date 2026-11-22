"""Bare-metal multi-tenant Ingress.

The Ingress owns the central account store, JWT/cookie authentication and the
routing table; each tenant runs a normal Sherry process (``SHERRY_HOME`` per
cell) behind loopback. The loopback Ingress connection is auth-exempt for HTTP;
WebSocket handshakes still present a single-use ticket minted by the cell.
"""
