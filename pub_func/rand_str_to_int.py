import hashlib

def rand_str_to_int(s: str, slice_len: int=8)-> int:
    # Hash with MD5 (or SHA256) and take the first N hex chars as an integer
    hash_value = hashlib.md5(s.encode('utf-8')).hexdigest()

    return int(hash_value[:slice_len], 16)