import os
import requests
from typing import List
from pathlib import Path
from config import ENV_PATH
from dotenv import load_dotenv

# 获取当前所在文件夹
current_dir = Path(__file__).parent.resolve()

# 加载环境变量
load_dotenv(ENV_PATH, override = True)
api_key = os.getenv("MINERU_KEY")

url = "https://mineru.net/api/v4/file-urls/batch"
header = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {api_key}"
}

class CustomExtractor:
    def invoke(self, paths:List[Path]):
        try:
            response = requests.post(
                url,
                headers=header,
                json={
                    "files": [{"name": data.name, "data_id": data.name} for data in paths],
                    "model_version": "pipeline"  # 使用视觉语言模型版本
                }
            )
            response.raise_for_status()

            result = response.json()
            if result["code"] == 0:
                batch_id = result["data"]["batch_id"]
                urls = result["data"]["file_urls"]

                for i in range(0, len(urls)):
                    with open(paths[i], 'rb') as f:  # 实际上传文件
                        res_upload = requests.put(urls[i], data=f)
                        res_upload.raise_for_status()
                        if res_upload.status_code == 200:
                            print(f"{urls[i]} upload success")
                        else:
                            print(f"{urls[i]} upload failed")
            else:
                print('apply upload url failed,reason:{}'.format(result["msg"]))
        except Exception as err:
            print(err)


extract_model = CustomExtractor()


