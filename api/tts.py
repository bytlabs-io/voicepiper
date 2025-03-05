# Copyright 202 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import threading
import wave
from dataclasses import dataclass
from queue import Queue

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    tts,
    utils,
)

from typing import Literal
import logging
from RealtimeTTS import TextToAudioStream, CoquiEngine
import requests

SERVER_URL = f"https://870c-34-73-159-234.ngrok-free.app/tts"

logger = logging.getLogger("livekit.plugins.konkonsa")

TTSModels = Literal["mist"]

TTSEncoding = Literal[
    "wav",
    "mp3",
]

ACCEPT_HEADER = {
    "wav": "audio/wav",
    "mp3": "audio/mp3",
}

play_text_to_speech_semaphore = threading.Semaphore(1)

@dataclass
class _TTSOptions:
    model: TTSModels | str
    speaker: str
    audio_format: TTSEncoding
    sample_rate: int
    speed_alpha: float
    reduce_latency: bool
    pause_between_brackets: bool
    phonemize_between_brackets: bool
    language: str



NUM_CHANNELS = 1


class TTS(tts.TTS):
    def __init__(
        self,
        *,
        model: TTSModels | str = "mist",
        language: str = "en",
        speaker: str = "lagoon",
        audio_format: TTSEncoding = "wav",
        sample_rate: int = 24000,
        speed_alpha: float = 1.0,
        reduce_latency: bool = False,
        pause_between_brackets: bool = False,
        phonemize_between_brackets: bool = False,
    ) -> None:
        """
        Create a new instance of Rime TTS.

        ``api_key`` must be set to your Rime API key, either using the argument or by setting the
        ``RIME_API_KEY`` environmental variable.

        Args:
            model: The TTS model to use. defaults to "mist"
            speaker: The speaker to use. defaults to "lagoon"
            audio_format: The audio format to use. defaults to "pcm"
            sample_rate: The sample rate to use. defaults to 16000
            speed_alpha: The speed alpha to use. defaults to 1.0
            reduce_latency: Whether to reduce latency. defaults to False
            pause_between_brackets: Whether to pause between brackets. defaults to False
            phonemize_between_brackets: Whether to phonemize between brackets. defaults to False
            api_key: The Rime API key to use.
            http_session: The HTTP session to use. defaults to a new session
        """
        super().__init__(
            capabilities=tts.TTSCapabilities(
                streaming=False,
            ),
            sample_rate=sample_rate,
            num_channels=NUM_CHANNELS,
        )

        self._opts = _TTSOptions(
            model=model,
            speaker=speaker,
            language=language,
            audio_format=audio_format,
            sample_rate=sample_rate,
            speed_alpha=speed_alpha,
            reduce_latency=reduce_latency,
            pause_between_brackets=pause_between_brackets,
            phonemize_between_brackets=phonemize_between_brackets,
        )

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        segment_id: str | None = None,
    ) -> "ChunkedStream":
        return ChunkedStream(
            tts=self,
            input_text=text,
            opts=self._opts,
            segment_id=segment_id,
            conn_options=conn_options,
        )

    def update_options(
        self,
        *,
        model: TTSModels | None,
        speaker: str | None,
    ) -> None:
        self._opts.model = model or self._opts.model
        self._opts.speaker = speaker or self._opts.speaker


class ChunkedStream(tts.ChunkedStream):
    """Synthesize using the chunked api endpoint"""

    def __init__(
        self,
        tts: TTS,
        input_text: str,
        opts: _TTSOptions,
        segment_id: str | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._opts = opts
        self._segment_id = segment_id or utils.shortuuid()
        self.blocksize = 512

    async def _run(self) -> None:
        stream = utils.audio.AudioByteStream(sample_rate=24000, num_channels=1)
        request_id = utils.shortuuid()
        try:
            response = requests.get(SERVER_URL, params={"text": self.input_text}, stream=True, timeout=10)
            response.raise_for_status()  # Raises an HTTPError if the response status is 4xx/5xx

            # Read data as it becomes available
            for chunk in response.iter_content(chunk_size=None):
                if chunk is None:
                    print("Terminating stream")
                    break
                for frame in stream.write(chunk):
                    self._event_ch.send_nowait(
                        tts.SynthesizedAudio(
                            request_id=request_id,
                            frame=frame,
                            segment_id=self._segment_id,
                        )
                    )
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientResponseError as e:
            raise APIStatusError(
                message=e.message,
                status_code=e.status,
                body=None,
            ) from e
        except Exception as e:
            raise APIConnectionError() from e


# def create_wave_header_for_engine(engine):
#     _, _, sample_rate = engine.get_stream_info()

#     num_channels = 1
#     sample_width = 2
#     frame_rate = sample_rate

#     wav_header = io.BytesIO()
#     with wave.open(wav_header, "wb") as wav_file:
#         wav_file.setnchannels(num_channels)
#         wav_file.setsampwidth(sample_width)
#         wav_file.setframerate(frame_rate)

#     wav_header.seek(0)
#     wave_header_bytes = wav_header.read()
#     wav_header.close()

#     # Create a new BytesIO with the correct MIME type for Firefox
#     final_wave_header = io.BytesIO()
#     final_wave_header.write(wave_header_bytes)
#     final_wave_header.seek(0)

#     return final_wave_header.getvalue()
