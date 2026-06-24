import sys
from pathlib import Path
from loguru import logger

# Dynamically add project root to sys.path
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import os
import json
import base64
import requests
from dotenv import load_dotenv
from config.path import STATIC_DIR
from pub_func import generate_tsid
from pydantic import validate_call

# Load environment variables
load_dotenv(project_root / ".env", override=True)

@validate_call
def generate_image(prompt: str) -> None:
    """
    Generate an image from a text description.

    Args:
        prompt: The user's text description for the image to generate.

    Returns:
        The file path of the saved image.
    """
    try:
        url = os.getenv("TTI_API_BASE")
        api_name = os.getenv("TTI_API_NAME")
        api_key = os.getenv("TTI_API_KEY")

        if not url:
            logger.info("Error: TTI_API_BASE environment variable not set")

        if not api_name:
            logger.info("Error: TTI_API_NAME environment variable not set")

        if not api_key:
            logger.info("Error: TTI_API_KEY environment variable not set")

        # Send request.
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
        data = {
            "model": api_name,
            "prompt": prompt,
            "size": "1024x1024",
            "response_format": "b64_json",
            "seed": 1
        }

        logger.info(f"Calling API to generate image...")
        logger.info(f"Using prompt: {prompt}")
        response = requests.post(url, headers=headers, data=json.dumps(data), verify=False)

        save_path = STATIC_DIR / "images" / f"{generate_tsid()}.png"

        # Ensure the directory exists
        save_path.parent.mkdir(parents=True, exist_ok=True)

        status_code = response.status_code
        if status_code == 200:
            # Parse the response
            response_data = response.json()
            if 'data' in response_data and len(response_data['data']) > 0:
                b64_data = response_data['data'][0]['b64_json']

                # Handle Data URL format
                if b64_data.startswith('data:'):
                    # Extract the actual base64 portion
                    # data:image/png;base64,iVBORw0KGgoAAA...
                    parts = b64_data.split(',', 1)
                    if len(parts) == 2:
                        b64_data = parts[1]
                        logger.info(f"Extracted pure base64 data, length: {len(b64_data)}")
                    else:
                        logger.warning(f"Warning: Unable to parse Data URL format: {b64_data[:50]}...")

                # Decode base64
                try:
                    image_data = base64.b64decode(b64_data)

                    # Save the image
                    with open(save_path, "wb") as f:
                        f.write(image_data)

                    logger.info(f"Image saved successfully at: {save_path}")
                    logger.info(f"File size: {save_path.stat().st_size} bytes")
                    logger.info(f"Generated: {save_path}")

                except Exception as decode_error:
                    logger.error(f"Base64 decode failed: {decode_error}")
                    logger.error(f"Base64 data length: {len(b64_data)}")

            else:
                logger.error(f"Unexpected API response format: {response_data}")
        else:
            logger.error(f"Request failed, status code: {status_code}")
            logger.error(f"Response body: {response.text}")

    except Exception as e:
        logger.error(f"Error occurred: {e}")
        import traceback
        traceback.print_exc()