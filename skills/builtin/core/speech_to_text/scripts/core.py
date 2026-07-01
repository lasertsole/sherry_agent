from loguru import  logger
from funasr import AutoModel
from config import MODELS_DIR
from pydantic import validate_call
from funasr.utils.postprocess_utils import rich_transcription_postprocess

model_dir = MODELS_DIR / "STT_model"

model = AutoModel(
    model = (model_dir / "model_weight").as_posix(),
    trust_remote_code = True,
    remote_code = (model_dir /"core.py").as_posix(),
    vad_model = "fsmn-vad",
    vad_kwargs = {"max_single_segment_time": 30000},
    device = "cuda:0",
)

@validate_call
def stt(audio_path: str)-> str:
    try:
        res = model.generate(
            input=audio_path,
            cache={},
            language="auto",
            use_itn=True,
            batch_size_s=60,
            merge_vad=True,  #
            merge_length_s=15,
        )
        text = rich_transcription_postprocess(res[0]["text"])
        suc_mes:str = f"Audio recognition completed, content:\n{text}"
        logger.info(suc_mes)
        return suc_mes
    except Exception as e:
        err_mes: str = f"[Error] Call failed: {e}"
        logger.error(err_mes)
        return err_mes