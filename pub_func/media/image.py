import io
import re
import time
import base64
import logging
import requests
from PIL import Image
from typing import Optional, Tuple

# 配置日志
logger = logging.getLogger(__name__)


def detect_image_format(image_data: bytes) -> Optional[str]:
    """通过文件头检测图片格式"""
    IMAGE_SIGNATURES = {
        b'\xFF\xD8\xFF': 'JPEG',
        b'\x89PNG\r\n\x1a\n': 'PNG',
        b'GIF87a': 'GIF',
        b'GIF89a': 'GIF',
        b'RIFF': 'WEBP',
        b'BM': 'BMP',
    }

    for signature, fmt in IMAGE_SIGNATURES.items():
        if image_data.startswith(signature):
            if fmt == 'WEBP' and len(image_data) > 12:
                if image_data[8:12] == b'WEBP':
                    return 'WEBP'
                else:
                    return None
            return fmt

    return None


def validate_and_fix_base64(base64_string: str) -> Optional[str]:
    """
    验证并修复base64字符串的padding

    Args:
        base64_string: base64编码的字符串

    Returns:
        修复后的base64字符串，如果无效则返回None
    """
    try:
        # 移除可能的data URI前缀
        if ',' in base64_string:
            prefix, base64_data = base64_string.split(',', 1)
        else:
            prefix = ""
            base64_data = base64_string

        # 移除空白字符
        base64_data = base64_data.strip()

        # 检查并修复padding
        missing_padding = len(base64_data) % 4
        if missing_padding:
            base64_data += '=' * (4 - missing_padding)
            logger.debug(f"修复base64 padding，添加了 {4 - missing_padding} 个 '='")

        # 验证base64是否有效
        test_decode = base64.b64decode(base64_data)

        # 重新组合
        if prefix:
            return f"{prefix},{base64_data}"
        else:
            return base64_data

    except Exception as e:
        logger.error(f"base64验证失败: {e}")
        return None


def download_and_convert_to_base64(url: str, timeout: int = 10, max_size_mb: int = 10) -> Optional[str]:
    """下载图片并转换为base64格式"""
    try:
        response = requests.get(url, timeout=timeout, stream=True)
        response.raise_for_status()

        # 检查文件大小
        content_length = response.headers.get('Content-Length')
        if content_length:
            size_mb = int(content_length) / (1024 * 1024)
            if size_mb > max_size_mb:
                logger.warning(f"图片过大: {size_mb:.2f}MB > {max_size_mb}MB")
                response.close()
                return None

        # 读取完整内容
        image_data = response.content
        response.close()

        # 验证图片数据不为空
        if not image_data or len(image_data) == 0:
            logger.error("下载的图片数据为空")
            return None

        # 检测实际格式
        detected_format = detect_image_format(image_data)
        if not detected_format:
            logger.warning("无法识别图片格式")
            return None

        # 转换为base64
        base64_bytes = base64.b64encode(image_data)
        base64_string = base64_bytes.decode('utf-8')

        # 验证base64字符串
        if not base64_string or len(base64_string) == 0:
            logger.error("base64转换结果为空")
            return None

        # 构建data URI格式
        mime_type = f'image/{detected_format.lower()}'
        if detected_format == 'JPEG':
            mime_type = 'image/jpeg'
        elif detected_format == 'WEBP':
            mime_type = 'image/webp'

        data_uri = f"data:{mime_type};base64,{base64_string}"

        # 验证并修复base64 padding
        validated_uri = validate_and_fix_base64(data_uri)
        if not validated_uri:
            logger.error("base64验证失败")
            return None

        logger.info(f"图片转换成功，格式: {detected_format}, base64长度: {len(validated_uri)} 字符")
        return validated_uri

    except Exception as e:
        logger.error(f"下载或转换失败: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return None


def compress_image_if_needed(image_data: bytes, max_size_mb: float = 5.0, max_dimension: int = 2048) -> bytes:
    """
    如果图片过大，则进行压缩

    Args:
        image_data: 原始图片数据
        max_size_mb: 最大允许的文件大小（MB）
        max_dimension: 最大边长（像素）

    Returns:
        压缩后的图片数据
    """
    try:
        # 检查原始大小
        original_size_mb = len(image_data) / (1024 * 1024)

        if original_size_mb <= max_size_mb:
            logger.info(f"图片大小合适: {original_size_mb:.2f}MB <= {max_size_mb}MB，无需压缩")
            return image_data

        logger.info(f"图片过大: {original_size_mb:.2f}MB > {max_size_mb}MB，开始压缩...")

        # 打开图片
        img = Image.open(io.BytesIO(image_data))
        original_width, original_height = img.size

        logger.info(f"原始尺寸: {original_width}x{original_height}")

        # 计算缩放比例
        scale = min(max_dimension / original_width, max_dimension / original_height, 1.0)

        if scale < 1.0:
            new_width = int(original_width * scale)
            new_height = int(original_height * scale)
            img = img.resize((new_width, new_height), Image.LANCZOS)
            logger.info(f"缩放后尺寸: {new_width}x{new_height} (比例: {scale:.2f})")

        # 转换为 RGB（如果是 RGBA 或 P 模式）
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
            logger.info("转换颜色模式为 RGB")

        # 保存为 JPEG（高压缩率）
        output_buffer = io.BytesIO()
        img.save(output_buffer, format='JPEG', quality=85, optimize=True)

        compressed_data = output_buffer.getvalue()
        compressed_size_mb = len(compressed_data) / (1024 * 1024)

        logger.info(f"✅ 压缩成功: {original_size_mb:.2f}MB -> {compressed_size_mb:.2f}MB")

        return compressed_data

    except Exception as e:
        logger.error(f"图片压缩失败: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        # 如果压缩失败，返回原始数据
        return image_data

def download_with_retry(url: str, timeout: int = 10, max_retries: int = 3) -> Optional[bytes]:
    """
    下载文件，带重试机制和正确的请求头

    Args:
        url: 下载URL
        timeout: 超时时间
        max_retries: 最大重试次数

    Returns:
        文件二进制数据，失败返回None
    """
    # 设置请求头，模拟浏览器请求
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://im.qq.com/',
        'Connection': 'keep-alive',
    }

    for attempt in range(max_retries):
        try:
            logger.info(f"尝试下载 (第 {attempt + 1}/{max_retries} 次): {url[:100]}...")

            response = requests.get(url, headers=headers, timeout=timeout, stream=True)

            # 检查HTTP状态码
            if response.status_code == 400:
                logger.error(f"HTTP 400 错误 - URL可能需要认证或已过期")
                logger.error(f"响应头: {dict(response.headers)}")
                return None
            elif response.status_code == 403:
                logger.error(f"HTTP 403 错误 - 访问被拒绝，可能需要Cookie")
                return None
            elif response.status_code == 404:
                logger.error(f"HTTP 404 错误 - 资源不存在")
                return None

            response.raise_for_status()

            # 读取完整内容
            image_data = response.content

            if len(image_data) == 0:
                logger.warning(f"下载内容为空 (尝试 {attempt + 1})")
                continue

            logger.info(f"下载成功，大小: {len(image_data)} bytes")
            return image_data

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP错误 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 指数退避
                logger.info(f"等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                return None
        except Exception as e:
            logger.error(f"下载失败 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return None

    return None

def clean_and_validate_base64(base64_string: str) -> Optional[str]:
    """
    清理并验证base64字符串，移除所有非法字符

    Args:
        base64_string: base64编码的字符串（可以是data URI格式或纯base64）

    Returns:
        清理后的base64字符串（data URI格式），如果无效则返回None
    """
    try:
        # 分离前缀和数据部分
        if ',' in base64_string:
            prefix, base64_data = base64_string.split(',', 1)
            # 验证前缀格式
            if not prefix.startswith('data:image/'):
                logger.warning(f"无效的data URI前缀: {prefix[:50]}")
                return None
        else:
            prefix = ""
            base64_data = base64_string

        # 关键修复：移除所有空白字符（空格、换行、制表符等）
        base64_data = re.sub(r'\s+', '', base64_data)

        # 只保留合法的base64字符和padding
        base64_data = re.sub(r'[^A-Za-z0-9+/=]', '', base64_data)

        if not base64_data:
            logger.error("base64数据为空")
            return None

        # 检查并修复padding
        # base64长度必须是4的倍数
        padding_needed = len(base64_data) % 4
        if padding_needed:
            base64_data += '=' * (4 - padding_needed)
            logger.debug(f"修复padding，添加了 {4 - padding_needed} 个 '='")

        # 验证base64是否可以正确解码
        try:
            decoded = base64.b64decode(base64_data, validate=True)
            if len(decoded) == 0:
                logger.error("base64解码后为空")
                return None
        except Exception as decode_err:
            logger.error(f"base64解码失败: {decode_err}")
            return None

        # 重新组合
        if prefix:
            result = f"{prefix},{base64_data}"
        else:
            result = base64_data

        logger.debug(f"base64验证成功，长度: {len(result)}, 解码后大小: {len(decoded)} bytes")
        return result

    except Exception as e:
        logger.error(f"base64清理验证失败: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return None

def download_and_convert_to_base64(url: str, timeout: int = 10, max_size_mb: int = 10) -> Optional[str]:
    """下载图片并转换为base64格式"""
    try:
        logger.info(f"开始处理图片URL: {url[:100]}...")

        # 使用带重试的下载函数
        image_data = download_with_retry(url, timeout)

        if not image_data:
            logger.error("下载失败或内容为空")
            return None

        # 检查文件大小
        size_mb = len(image_data) / (1024 * 1024)
        if size_mb > max_size_mb:
            logger.warning(f"图片过大: {size_mb:.2f}MB > {max_size_mb}MB")
            return None

        logger.info(f"图片下载成功，大小: {len(image_data)} bytes ({size_mb:.2f}MB)")

        # 检测实际格式
        detected_format = detect_image_format(image_data)
        if not detected_format:
            logger.warning(f"无法识别图片格式，文件头: {image_data[:20].hex()}")
            logger.warning("将尝试继续处理...")
            detected_format = 'JPEG'

        logger.info(f"检测到图片格式: {detected_format}")

        # 压缩图片（如果过大）
        compressed_data = compress_image_if_needed(
            image_data,
            max_size_mb=5.0,  # 压缩到 5MB 以内
            max_dimension=2048  # 最大边长 2048px
        )

        compressed_size_mb = len(compressed_data) / (1024 * 1024)
        logger.info(f"压缩后大小: {compressed_size_mb:.2f}MB")

        # 转换为base64（不使用换行）
        base64_bytes = base64.b64encode(compressed_data)
        base64_string = base64_bytes.decode('utf-8')

        # 验证base64字符串
        if not base64_string or len(base64_string) == 0:
            logger.error("base64转换结果为空")
            return None

        # 构建data URI格式
        mime_type = 'image/jpeg'  # 压缩后统一使用 JPEG
        data_uri = f"data:{mime_type};base64,{base64_string}"

        logger.info(f"构建data URI，总长度: {len(data_uri)} 字符")

        # 清理并验证base64
        validated_uri = clean_and_validate_base64(data_uri)
        if not validated_uri:
            logger.error("base64验证失败")
            return None

        logger.info(f"✅ 图片转换成功，格式: JPEG, base64长度: {len(validated_uri)} 字符")
        return validated_uri

    except Exception as e:
        logger.error(f"下载或转换失败: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return None

    except Exception as e:
        logger.error(f"检查失败: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return False, None, None, None

def check_if_image_and_convert_to_base64(url: str, timeout: int = 10, max_size_mb: int = 10) -> Tuple[
    bool, Optional[str], Optional[str], Optional[str]]:
    """
    综合判断URL是否为图片，如果是则转换为base64

    Args:
        url: 图片URL
        timeout: 请求超时时间（秒）
        max_size_mb: 最大允许的图片大小（MB），防止下载过大的文件

    Returns:
        (is_image, content_type, file_format, base64_string)
    """
    try:
        # 对于QQ多媒体URL，直接尝试下载，跳过HEAD请求
        if 'multimedia.nt.qq.com.cn' in url or 'qq.com' in url:
            logger.info("检测到QQ多媒体URL，直接尝试下载...")

            # 直接下载并转换
            base64_str = download_and_convert_to_base64(url, timeout, max_size_mb)

            if base64_str:
                return True, 'image/jpeg', 'JPEG', base64_str
            else:
                return False, None, None, None

        # 非QQ URL，使用标准流程
        response = requests.head(url, timeout=timeout, allow_redirects=True)
        content_type = response.headers.get('Content-Type', '').lower()

        content_length = response.headers.get('Content-Length')
        if content_length:
            size_mb = int(content_length) / (1024 * 1024)
            if size_mb > max_size_mb:
                logger.warning(f"图片过大: {size_mb:.2f}MB > {max_size_mb}MB")
                return False, content_type, None, None

        if 'image/' in content_type:
            file_format = content_type.split('/')[-1].upper()
            logger.info(f"Content-Type显示为图片: {content_type}, 格式: {file_format}")

            base64_str = download_and_convert_to_base64(url, timeout, max_size_mb)
            if base64_str:
                return True, content_type, file_format, base64_str
            else:
                return False, content_type, file_format, None

        logger.debug("Content-Type不明确，检查文件头...")
        response = requests.get(url, timeout=timeout, stream=True)
        response.raise_for_status()

        header = response.raw.read(32)

        IMAGE_SIGNATURES = {
            b'\xFF\xD8\xFF': 'JPEG',
            b'\x89PNG\r\n\x1a\n': 'PNG',
            b'GIF87a': 'GIF',
            b'GIF89a': 'GIF',
            b'RIFF': 'WEBP',
            b'BM': 'BMP',
        }

        detected_format = None
        for signature, fmt in IMAGE_SIGNATURES.items():
            if header.startswith(signature):
                detected_format = fmt
                break

        if not detected_format:
            logger.debug(f"不是图片，Content-Type: {content_type}")
            response.close()
            return False, content_type, None, None

        response.close()
        base64_str = download_and_convert_to_base64(url, timeout, max_size_mb)

        if base64_str:
            content_type = f'image/{detected_format.lower()}'
            return True, content_type, detected_format, base64_str
        else:
            return False, content_type, detected_format, None

    except Exception as e:
        logger.error(f"检查失败: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return False, None, None, None