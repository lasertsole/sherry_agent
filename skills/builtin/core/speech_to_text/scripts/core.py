from loguru import  logger
from funasr import AutoModel
from config import MODELS_DIR
from funasr.utils.postprocess_utils import rich_transcription_postprocess

model_dir = MODELS_DIR / "SST_model"

model = AutoModel(
    model = (model_dir / "model_weight").as_posix(),
    trust_remote_code = True,
    remote_code = (model_dir /"model.py").as_posix(),
    vad_model = "fsmn-vad",
    vad_kwargs = {"max_single_segment_time": 30000},
    device = "cuda:0",
)

def stt(audio_path: str)-> None:
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
        logger.info(f"Audio recognition completed, content:\n{text}")
    except Exception as e:
        logger.error(f"[Error] Call failed: {e}")