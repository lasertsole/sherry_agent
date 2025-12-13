import hashlib

def rand_str_to_int(s: str, slice_len: int=8)-> int:
    # 使用 MD5 或 SHA256 生成哈希，取前8位转为整数
    hash_value = hashlib.md5(s.encode('utf-8')).hexdigest()

    return int(hash_value[:slice_len], 16)  # 取前8个十六进制字符转为十进制整数