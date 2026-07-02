import hashlib

def string_to_unique_int(s: str) -> int:
    """Convert a string to a unique integer (SHA-256 based)."""
    hash_object = hashlib.sha256(s.encode('utf-8'))
    # Take the first 8 bytes as a 64-bit integer
    return int.from_bytes(hash_object.digest()[:8], byteorder='big')
