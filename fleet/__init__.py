"""Bare-metal multi-tenant Ingress (P2).

The Ingress owns the central account store, JWT/cookie authentication and the
routing table; each tenant runs a normal Sherry process (``SHERRY_HOME`` per
cell) behind loopback. Cells stay auth-exempt for the Ingress connection.
"""
