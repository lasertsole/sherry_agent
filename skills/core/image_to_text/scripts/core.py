import sys
from pathlib import Path

# 动态添加项目根目录到 sys.path
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import base64
from PIL import Image
from models import vl_model
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

# 加载环境变量
load_dotenv(project_root / ".env", override=True)


def itt(image_path: str, user_text: str = "请尽可能详细的描述图片中的内容。")-> None:
    """检测文件是否是有效的图片"""
    path = Path(image_path)

    try:
        # 检查文件是否存在
        if not path.exists():
            print(f"文件不存在: {image_path}")
            return None

        # 尝试用 PIL 打开，能打开就是有效图片
        with Image.open(path) as img:
            img.verify()  # 验证图片完整性
    except Exception:
        print(f"该文件不是有效图片: {image_path}")
        return None

    """将图片转换为 base64 字符串"""
    try:
        # 获取图片格式
        with Image.open(path) as img:
            img_format = img.format.lower()  # jpg, png, webp 等

        # 读取图片并转换为 base64
        with open(path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')

    except Exception as e:
        print(f"❌ 图片转换失败: {e}")
        return None

    try:
        # 返回 data URI 格式
        image_base64: str = f"data:image/{img_format};base64,{encoded_string}"
        content_list: list[dict[str, str]] = [{"type": "text", "text": user_text}, {"type": "image_url", "image_url": {"url": image_base64}}]

        res = vl_model.invoke([HumanMessage(content = content_list)])

        print("图片识别完成，内容为:\n", res.content)
        return None
    except Exception as e:
        print(f"❌ 视觉模型图片失败: {e}")