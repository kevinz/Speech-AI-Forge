if __name__ == "__main__":
    from modules.repos_static.sys_paths import setup_repos_paths

    setup_repos_paths()

import logging
import threading
from functools import partial
from pathlib import Path
from typing import Generator, Optional

import librosa
import numpy as np
import torch
from hyperpyyaml import load_hyperpyyaml

from modules.core.models.AudioReshaper import AudioReshaper
from modules.core.models.tts.CosyVoiceFE import CosyVoiceFrontEnd
from modules.core.models.TTSModel import TTSModel
from modules.core.pipeline.dcls import TTSPipelineContext, TTSSegment
from modules.core.pipeline.processor import NP_AUDIO
from modules.core.spk import TTSSpeaker
from modules.devices import devices
from modules.repos_static.cosyvoice.cosyvoice.cli.model import CosyVoiceModel
from modules.utils import audio_utils
from modules.utils.SeedContext import SeedContext

max_val = 0.8
prompt_sr, target_sr = 16000, 16000


def postprocess(speech: np.ndarray, top_db=60, hop_length=220, win_length=440):
    speech, _ = librosa.effects.trim(
        speech, top_db=top_db, frame_length=win_length, hop_length=hop_length
    )
    if speech.abs().max() > max_val:
        speech = speech / speech.abs().max() * max_val
    speech = torch.concat([speech, torch.zeros(1, int(target_sr * 0.2))], dim=1)
    return speech


class CosyVoiceTTSModel(TTSModel):
    logger = logging.getLogger(__name__)

    load_lock = threading.Lock()

    model: Optional[CosyVoiceModel] = None
    frontend: Optional[CosyVoiceFrontEnd] = None

    def __init__(self) -> None:
        super().__init__("cosy-voice")

        paths = [
            Path("./models/CosyVoice_300M"),
            Path("./models/CosyVoice_300M_Instruct"),
            Path("./models/CosyVoice_300M_SFT"),
        ]
        paths = [p for p in paths if p.exists()]
        if len(paths) == 0:
            paths = [Path("./models/CosyVoice_300M")]
            self.logger.info("No CosyVoice model found")
        else:
            self.logger.info(f"Found CosyVoice model: {paths}")

        self.model_dir = paths[0]

        self.device = devices.get_device_for(self.model_id)
        self.dtype = devices.dtype

        self.model = CosyVoiceTTSModel.model
        self.frontend = CosyVoiceTTSModel.frontend

    def is_loaded(self) -> bool:
        return CosyVoiceTTSModel.model is not None

    def reset(self) -> None:
        return super().reset()

    def load(
        self, context: TTSPipelineContext = None
    ) -> tuple[CosyVoiceModel, CosyVoiceFrontEnd]:
        with self.load_lock:
            if CosyVoiceTTSModel.model is not None:
                return CosyVoiceTTSModel.model, CosyVoiceTTSModel.frontend
            self.logger.info("Loading CosyVoice model...")

            device = self.device
            dtype = self.dtype
            model_dir = self.model_dir

            with open(model_dir / "cosyvoice.yaml", "r") as f:
                configs = load_hyperpyyaml(f)

            frontend = CosyVoiceFrontEnd(
                get_tokenizer=configs["get_tokenizer"],
                feat_extractor=configs["feat_extractor"],
                campplus_model=model_dir / "campplus.onnx",
                speech_tokenizer_model=model_dir / "speech_tokenizer_v1.onnx",
                spk2info=model_dir / "spk2info.pt",
                instruct=True,
                allowed_special=configs["allowed_special"],
            )
            frontend.device = device
            self.frontend = frontend

            model = CosyVoiceModel(configs["llm"], configs["flow"], configs["hift"])
            model.device = device
            model.load(
                llm_model=model_dir / "llm.pt",
                flow_model=model_dir / "flow.pt",
                hift_model=model_dir / "hift.pt",
            )
            model.llm.to(device=device, dtype=dtype)
            model.flow.to(device=device, dtype=dtype)
            model.hift.to(device=device, dtype=dtype)

            self.model = model

            devices.torch_gc()
            self.logger.info("CosyVoice model loaded.")

            CosyVoiceTTSModel.model = model
            CosyVoiceTTSModel.frontend = frontend

            return model, frontend

    def unload(self, context: TTSPipelineContext = None) -> None:
        with self.load_lock:
            if CosyVoiceTTSModel.model is None:
                return
            del self.model
            del self.frontend
            self.model = None
            self.frontend = None
            del CosyVoiceTTSModel.model
            del CosyVoiceTTSModel.frontend
            CosyVoiceTTSModel.model = None
            CosyVoiceTTSModel.frontend = None
            devices.torch_gc()

    def encode(self, text: str) -> list[int]:
        from whisper.tokenizer import Tokenizer

        self.load()
        tokenizer: Tokenizer = self.frontend.tokenizer
        return tokenizer.encode(text)

    def decode(self, ids: list[int]) -> str:
        from whisper.tokenizer import Tokenizer

        self.load()
        tokenizer: Tokenizer = self.frontend.tokenizer
        return tokenizer.decode(ids)

    def inference_sft(self, tts_texts: list[str], spk_embedding: torch.Tensor):
        tts_speeches = []
        for text in tts_texts:
            model_input = self.frontend.frontend_sft(
                tts_text=text, spk_embedding=spk_embedding
            )
            model_output = self.model.inference(**model_input)
            tts_speeches.append(model_output["tts_speech"])
        return {"tts_speech": torch.concat(tts_speeches, dim=1)}

    def inference_zero_shot(
        self, tts_texts: list[str], prompt_text: str, prompt_speech_16k: torch.Tensor
    ):
        tts_speeches = []
        for text in tts_texts:
            model_input = self.frontend.frontend_zero_shot(
                text, prompt_text, prompt_speech_16k
            )
            model_output = self.model.inference(**model_input)
            tts_speeches.append(model_output["tts_speech"])
        return {"tts_speech": torch.concat(tts_speeches, dim=1)}

    def inference_cross_lingual(
        self, tts_texts: list[str], prompt_speech_16k: torch.Tensor
    ):
        if self.frontend.instruct is True:
            raise ValueError(
                "{} do not support cross_lingual inference".format(self.model_dir)
            )
        tts_speeches = []
        for text in tts_texts:
            model_input = self.frontend.frontend_cross_lingual(text, prompt_speech_16k)
            model_output = self.model.inference(**model_input)
            tts_speeches.append(model_output["tts_speech"])
        return {"tts_speech": torch.concat(tts_speeches, dim=1)}

    def inference_instruct(
        self, tts_texts: list[str], spk_embedding: torch.Tensor, instruct_text: str
    ):
        if self.frontend.instruct is False:
            raise ValueError(
                "{} do not support instruct inference".format(self.model_dir)
            )
        tts_speeches = []
        for text in tts_texts:
            model_input = self.frontend.frontend_instruct(
                tts_text=text, spk_embedding=spk_embedding, instruct_text=instruct_text
            )
            model_output = self.model.inference(**model_input)
            tts_speeches.append(model_output["tts_speech"])
        return {"tts_speech": torch.concat(tts_speeches, dim=1)}

    def spk_to_embedding(self, spk: TTSSpeaker):
        token_cfg = (
            spk.get_token(self.model_id)
            or spk.get_token("cosyvoice_300m_instruct")
            or spk.get_token("cosyvoice_instruct")
        )
        if token_cfg is None:
            return None
        if len(token_cfg.embedding) > 0:
            return token_cfg.embedding[0].unsqueeze(0)
        return None

    def spk_to_ref_wav(self, spk: TTSSpeaker, emotion: str = ""):
        ref_data = spk.get_ref(lambda x: x.emotion == emotion)
        if ref_data is None:
            return None, None
        wav = audio_utils.bytes_to_librosa_array(
            audio_bytes=ref_data.wav, sample_rate=ref_data.wav_sr
        )
        _, wav = AudioReshaper.normalize_audio(
            audio=(ref_data.wav_sr, wav), target_sr=target_sr
        )
        return wav, ref_data.text

    def generate_batch(
        self, segments: list[TTSSegment], context: TTSPipelineContext
    ) -> list[NP_AUDIO]:
        return next(self.generate_batch_stream(segments, context))

    def generate_batch_stream(
        self, segments: list[TTSSegment], context: TTSPipelineContext
    ) -> Generator[list[NP_AUDIO], None, None]:
        cached = self.get_cache(segments=segments, context=context)
        if cached is not None:
            yield cached
            return

        # NOTE: 因为不支持流式，所以是同步的

        self.load()

        seg0 = segments[0]
        spk = seg0.spk

        if spk is None:
            # TODO: 可以使用随机的 spk
            raise ValueError("spk is None")

        # TODO 目前不能传递这些值
        temperature = seg0.temperature
        top_p = seg0.top_p
        top_k = seg0.top_k

        emotion = seg0.emotion
        prompt2 = seg0.prompt2
        infer_seed = seg0.infer_seed

        instruct_text = emotion or prompt2 or ""

        if spk.gender == "female" or "女" in spk.gender:
            instruct_text = f"female voice. {instruct_text}"
        else:
            instruct_text = f"male voice. {instruct_text}"

        spk_embedding = self.spk_to_embedding(spk) if spk else None
        ref_wav, ref_text = (
            self.spk_to_ref_wav(spk, emotion=emotion) if spk else (None, None)
        )

        infer_func: callable = None
        if spk_embedding is not None:
            # NOTE: 如果 spk_embedding 不为空，则直接使用
            # infer_func = partial(self.inference_sft, spk_embedding=spk_embedding)
            infer_func = partial(
                self.inference_instruct,
                spk_embedding=spk_embedding,
                instruct_text=instruct_text,
            )
        elif ref_wav is not None and ref_text:
            # NOTE: 如果 ref_wav 和 ref_text 都不为空，则使用 zero-shot
            infer_func = partial(
                self.inference_zero_shot,
                prompt_text=ref_text,
                prompt_speech_16k=torch.from_numpy(ref_wav).unsqueeze(0),
            )
        else:
            raise ValueError("spk_embedding is None")
            # NOTE: 否则使用 inference_instruct

        # NOTE: 迷，不是很清楚为什么输入要 16k 输出却是 22050 ...
        sr = 22050

        results: list[NP_AUDIO] = []
        for seg in segments:
            if context.stop:
                break

            with SeedContext(infer_seed):
                result = infer_func(tts_texts=[seg.text])
            wav = result["tts_speech"].float().cpu().numpy().squeeze()
            results.append((sr, wav))

        self.set_cache(segments=segments, context=context, value=results)
        yield results


if __name__ == "__main__":
    import soundfile as sf
    import tqdm
    from whisper.tokenizer import Tokenizer

    from modules.core.pipeline.dcls import TTSSegment
    from modules.core.spk import spk_mgr

    model = CosyVoiceTTSModel()
    model.logger.setLevel(logging.DEBUG)
    model.load()

    t1 = "我们走的每一步，都是我们策略的一部分；你看到的所有一切，包括我此刻与你交谈，所做的一切，所说的每一句话，都有深远的含义。"
    t2 = "你好，此语音使用 Cosy Voice 合成。"
    text = t1

    emotion = "A younger female speaker with normal pitch, slow speaking rate like whispering, and gentle emotion."

    def gen_audio(spk_name: str):
        spk = spk_mgr.get_speaker(spk_name)
        print("spk.id", spk.id)

        sr, audio_data = model.generate(
            segment=TTSSegment(_type="text", text=text, spk=spk, emotion=emotion),
            context=None,
        )
        # audio_data = (audio_data * (2**15)).astype(np.int16)
        sf.write(f"test_cosyvoice{spk_name}.wav", audio_data, sr, format="WAV")

    spk_names = ["中文女", "中文男", "英文女", "粤语女", "韩语女"]

    for name in tqdm.tqdm(spk_names):
        gen_audio(name)
