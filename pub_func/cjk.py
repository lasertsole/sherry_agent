def contains_cjk(text: str) -> bool:
    """Check if text contains CJK (Chinese, Japanese, Korean) characters."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or    # CJK Extension A
            0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
            0x3000 <= cp <= 0x303F or    # CJK Symbols
            0x3040 <= cp <= 0x309F or    # Hiragana
            0x30A0 <= cp <= 0x30FF or    # Katakana
            0xAC00 <= cp <= 0xD7AF):     # Hangul Syllables
            return True
    return False

def is_cjk_codepoint(cp: int) -> bool:
    return (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or    # CJK Extension A
            0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
            0x3000 <= cp <= 0x303F or    # CJK Symbols
            0x3040 <= cp <= 0x309F or    # Hiragana
            0x30A0 <= cp <= 0x30FF or    # Katakana
            0xAC00 <= cp <= 0xD7AF)      # Hangul Syllables

def count_cjk(text: str) -> int:
    """Count CJK characters in text."""
    return sum(1 for ch in text if is_cjk_codepoint(ord(ch)))