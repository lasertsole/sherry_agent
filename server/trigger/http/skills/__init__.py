"""Skill HTTP API — per-resource controllers.

Importing this package registers the skill routes (same set, same order as
the former single-module ``skills.py``): catalog (list/read), lifecycle
(upload/toggle) and auto-skill management (delete/pin).
"""

from server.trigger.http.skills._shared import _json_response as _json_response

from server.trigger.http.skills import catalog as catalog
from server.trigger.http.skills import lifecycle as lifecycle
from server.trigger.http.skills import manage as manage
