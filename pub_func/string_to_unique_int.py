import hashlib

def string_to_unique_int(s: str) -> int:
    """将字符串转换为唯一整数（基于 SHA-256 哈希）"""
    # 计算 SHA-256 哈希值
    hash_object = hashlib.sha256(s.encode('utf-8'))
    # 取前 8 个字节转换为整数（64 位整数）
    return int.from_bytes(hash_object.digest()[:8], byteorder='big')
