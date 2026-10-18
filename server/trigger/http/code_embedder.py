"""HTTP surface for the optional code-intel embedder download/status."""

from server.service import code_embedder_service as service
from server.trigger.core import app


@app.get("/code-embedder/status")
async def code_embedder_status_handler(request) -> dict:
    """Download/enable state of the optional code-intel embedder."""
    return service.status()


@app.post("/code-embedder/download")
async def code_embedder_download_handler(request) -> dict:
    """Start the background download; the UI polls /code-embedder/status."""
    return service.start_download()


@app.delete("/code-embedder")
async def code_embedder_disable_handler(request) -> dict:
    """Disable the semantic layer (the model files stay on disk)."""
    return service.disable()
