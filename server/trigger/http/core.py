import json
from robyn import Robyn
from loguru import logger
from requests import Response
from robyn.status_codes import HTTP_500_INTERNAL_SERVER_ERROR

# Create the app
app = Robyn(__file__)

@app.exception
def handle_session_end(error: Exception) -> Response:
    """
        Global exception interceptor
        Called when any uncaught exception is raised inside route handlers
        """
    # Log the error for debugging
    logger.exception(error)

    # Return a uniform JSON error response
    return Response(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        headers={"Content-Type": "application/json"},
        description=json.dumps({
            "success": False,
            "message": "Internal Server Error",
            "error": str(error)
        }, ensure_ascii=False)
    )