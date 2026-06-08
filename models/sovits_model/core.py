import os
import requests
from pathlib import Path
from typing import Union
from loguru import logger
from config import ENV_PATH
from requests import Response
from dotenv import load_dotenv
from pydantic import BaseModel

_current_dir = Path(__file__).parent.resolve()

#参考语音地址
_refer_audio_path = _current_dir / 'src/refer_audio.ogg'
_refer_audio_path = _refer_audio_path.resolve().as_posix()

#参考文本地址
_refer_text_path = _current_dir / "src/refer_text.txt"
_refer_text_path = _refer_text_path.resolve().as_posix()

#辅助语言地址列表
_aux_ref_audio_folder_path = _current_dir / "src/aux_ref_audio"
aux_ref_audio_path_list = [(_aux_ref_audio_folder_path / f.name).as_posix() for f in _aux_ref_audio_folder_path.iterdir() if f.is_file()]

with open(_refer_text_path, "r", encoding="utf-8") as f:
    _refer_text = f.read()

"""
### 推理请求参数
{
    "text": "",                   # str.(required) text to be synthesized
    "text_lang: "",               # str.(required) language of the text to be synthesized
    "ref_audio_path": "",         # str.(required) reference audio path
    "aux_ref_audio_paths": [],    # list.(optional) auxiliary reference audio paths for multi-speaker tone fusion
    "prompt_text": "",            # str.(optional) prompt text for the reference audio
    "prompt_lang": "",            # str.(required) language of the prompt text for the reference audio
    "top_k": 15,                  # int. top k sampling
    "top_p": 1,                   # float. top p sampling
    "temperature": 1,             # float. temperature for sampling
    "text_split_method": "cut5",  # str. text split method, see text_segmentation_method.py for details.
    "batch_size": 1,              # int. batch size for inference
    "batch_threshold": 0.75,      # float. threshold for batch splitting.
    "split_bucket": True,         # bool. whether to split the batch into multiple buckets.
    "speed_factor":1.0,           # float. control the speed of the synthesized audio.
    "fragment_interval":0.3,      # float. to control the interval of the audio fragment.
    "seed": -1,                   # int. random seed for reproducibility.
    "parallel_infer": True,       # bool. whether to use parallel inference.
    "repetition_penalty": 1.35,   # float. repetition penalty for T2S model.
    "sample_steps": 32,           # int. number of sampling steps for VITS model V3.
    "super_sampling": False,      # bool. whether to use super-sampling for audio when using VITS model V3.
    "streaming_mode": False,      # bool or int. return audio chunk by chunk.T he available options are: 0,1,2,3 or True/False (0/False: Disabled | 1/True: Best Quality, Slowest response speed (old version streaming_mode) | 2: Medium Quality, Slow response speed | 3: Lower Quality, Faster response speed )
    "overlap_length": 2,          # int. overlap length of semantic tokens for streaming mode.
    "min_chunk_length": 16,       # int. The minimum chunk length of semantic tokens for streaming mode. (affects audio chunk size)
}
"""
class TTS_Request(BaseModel):
    text: str = None
    text_lang: str = None
    ref_audio_path: str = _refer_audio_path
    aux_ref_audio_paths: list = aux_ref_audio_path_list
    prompt_lang: str = 'ja'
    prompt_text: str = _refer_text
    top_k: int = 5
    top_p: float = 1
    temperature: float = 1
    text_split_method: str = "cut5"
    batch_size: int = 20
    batch_threshold: float = 0.75
    split_bucket: bool = True
    speed_factor: float = 1.0
    fragment_interval: float = 0.3
    seed: int = -1
    streaming_mode: Union[bool, int] = False
    parallel_infer: bool = True
    repetition_penalty: float = 1.35
    sample_steps: int = 32
    super_sampling: bool = False
    overlap_length: int = 2
    min_chunk_length: int = 16

_base_url = "http://127.0.0.1:9880"
_control_url = _base_url + "/control"
_tts_url = _base_url + "/tts"
_change_GPT_url = _base_url + "/set_gpt_weights"
_change_sovits_url = _base_url + "/set_sovits_weights"
_change_refer_audio_url = _base_url + "/set_refer_audio"

# 加载环境变量
load_dotenv(ENV_PATH, override=True)

# 获取gpt-sovits项目所在文件夹
_gpt_sovits_dir = os.getenv("GPT_SOVITS_DIR")
_gpt_sovits_dir = Path(_gpt_sovits_dir)

# 获取gpt-sovits api路径
_api_path = _gpt_sovits_dir / "api_v2.py"
_api_path = _api_path.as_posix()

# 获取gpt-sovits解释器路径
_interpreter_path = _gpt_sovits_dir / 'runtime/python.exe'
_interpreter_path = _interpreter_path.as_posix()

# 获取gpt-sovits 配置路径
_config_path = Path(__file__).parent.resolve() / "config/tts_infer.yaml"
_config_path = _config_path.as_posix()

# 模型gpt权重路径
_gpt_weight_path = os.getenv("GPT_WEIGHT_PATH")
_gpt_weight_path = _gpt_sovits_dir / _gpt_weight_path
_gpt_weight_path = _gpt_weight_path.as_posix()

# 模型sovits权重路径
_sovits_weight_path = os.getenv("SOVITS_WEIGHT_PATH")
_sovits_weight_path = _gpt_sovits_dir / _sovits_weight_path
_sovits_weight_path = _sovits_weight_path.as_posix()

"""以下是api"""
initialed = False
### 推理
def fetch_TTS_sound(request: TTS_Request)-> Response | None:
    global initialed
    try:
        if initialed:
            res = requests.post(_tts_url, json=request.model_dump(), verify=True)
            return res
        else:
            res = change_GPT_model(_gpt_weight_path)
            res.raise_for_status()

            res = change_sovits_model(_sovits_weight_path)
            res.raise_for_status()

            res = requests.post(_tts_url, json=request.model_dump(), verify=True)
            res.raise_for_status()
            initialed = True
            return res

    except Exception as e:
        logger.error(f"Error in fetch_TTS_sound: {e}")
        return None


'''
### 命令控制

command:
"restart": 重新运行
"exit": 结束运行
'''
def control_model(command: str = None)-> Response | None:
    if command is None or (command != "restart" and command != "exit"):
        return None
    payload = {
        "command": command
    }

    res = requests.get(_control_url, params=payload, verify=True)
    return res


### 切换GPT模型
def change_GPT_model(weights_path: str = None)-> Response | None:
    if weights_path is None:
        return None

    payload = {
        "weights_path": weights_path
    }
    res = requests.get(_change_GPT_url, params=payload, verify=True)
    return res


### 切换sovits模型
def change_sovits_model(weights_path: str = None)-> Response | None:
    if weights_path is None:
        return None

    payload = {
        "weights_path": weights_path
    }
    res = requests.get(_change_sovits_url, params=payload, verify=True)
    return res


### 切换参考音频
def change_refer_audio(refer_audio_path: str = None)-> Response | None:
    if refer_audio_path is None:
        return None

    payload = {
        refer_audio_path: refer_audio_path
    }

    res = requests.get(_change_refer_audio_url, params=payload, verify=True)
    return res