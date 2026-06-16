from .core import app

# Let top-level code run (imports needed for side-effect registration)
import server.trigger.http.messages
import server.trigger.http.workplace