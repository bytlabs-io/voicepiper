# Copyright 2025 LiveKit, Inc.
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
import dataclasses
import json
import os
import weakref
from typing import Dict, List, Optional

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    APIStatusError,
    stt,
    utils,
)
from livekit.agents.utils import AudioBuffer

from .audio_recorder_client import AudioToTextRecorderClient

from .log import logger
from .types import (
    AudioSettings,
    ClientMessageType,
    ConnectionSettings,
    ServerMessageType,
    TranscriptionConfig,
)


class STT(stt.STT):
    def __init__(
        self,
        *,
        transcription_config: TranscriptionConfig = TranscriptionConfig(
            language="en",
            operating_point="enhanced",
            enable_partials=True,
            max_delay=0.7,
        ),
        connection_settings: ConnectionSettings = ConnectionSettings(
            url="wss://dolphin-rare-buck.ngrok-free.app/ws",
        ),
        audio_settings: AudioSettings = AudioSettings(),
        http_session: Optional[aiohttp.ClientSession] = None,
        extra_headers: Optional[Dict] = None,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
            ),
        )
        self._transcription_config = transcription_config
        self._audio_settings = audio_settings
        self._connection_settings = connection_settings
        self._extra_headers = extra_headers or {}
        self._session = http_session
        self._streams = weakref.WeakSet[SpeechStream]()

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: str | None,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise NotImplementedError("Not implemented")

    def stream(
        self,
        *,
        language: Optional[str] = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "SpeechStream":
        config = dataclasses.replace(self._audio_settings)
        stream = SpeechStream(
            stt=self,
            transcription_config=self._transcription_config,
            audio_settings=config,
            connection_settings=self._connection_settings,
            conn_options=conn_options,
            http_session=self.session,
            extra_headers=self._extra_headers,
        )
        self._streams.add(stream)
        return stream


class SpeechStream(stt.SpeechStream):
    def __init__(
        self,
        *,
        stt: STT,
        transcription_config: TranscriptionConfig,
        audio_settings: AudioSettings,
        connection_settings: ConnectionSettings,
        conn_options: APIConnectOptions,
        http_session: aiohttp.ClientSession,
        extra_headers: Optional[Dict] = None,
    ) -> None:
        super().__init__(
            stt=stt, conn_options=conn_options, sample_rate=audio_settings.sample_rate
        )
        self._transcription_config = transcription_config
        self._audio_settings = audio_settings
        self._connection_settings = connection_settings
        self._session = http_session
        self._extra_headers = extra_headers or {}
        self._speech_duration: float = 0

        self._reconnect_event = asyncio.Event()
        self._recognition_started = asyncio.Event()
        self._seq_no = 0

    async def _run(self):
        closing_ws = False


        async def send_task(client: AudioToTextRecorderClient):
            nonlocal closing_ws
            print("sending audio to")

            # start_recognition_msg = {
            #     "message": ClientMessageType.StartRecognition,
            #     "audio_format": self._audio_settings.asdict(),
            #     "transcription_config": self._transcription_config.asdict(),
            # }

            audio_bstream = utils.audio.AudioByteStream(
                sample_rate=self._audio_settings.sample_rate,
                num_channels=1,
            )

            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    frames = audio_bstream.flush()
                else:
                    frames = audio_bstream.write(data.data.tobytes())

                for frame in frames:
                    self._seq_no += 1
                    self._speech_duration += frame.duration
                    client.feed_audio(frame.data.tobytes())

            closing_ws = True

        def recv_task(text, loop = None):
            print("This is the text: %s" % text)
            nonlocal closing_ws
            try:
                data = {
                    "type": ServerMessageType.AddTranscript,
                    "seq_no": self._seq_no,
                    "text": text,
                    "duration": self._speech_duration,
                    "transcription_config": self._transcription_config.asdict(),
                    "audio_settings": self._audio_settings.asdict(),
                }
                self._process_stream_event(data, closing_ws)
            except Exception:
                logger.exception("failed to process Speechmatics message")

        ws: aiohttp.ClientWebSocketResponse | None = None

        while True:
            try:

                client = AudioToTextRecorderClient(
                    language="en",
                    control_url="wss://dolphin-rare-buck.ngrok-free.app",
                    data_url="wss://f434-34-168-123-239.ngrok-free.app",
                    debug_mode=True,
                    on_realtime_transcription_update=recv_task,
                    use_microphone=False,
                )
                while True:
                    tasks = [
                        asyncio.create_task(send_task(client)),
                        # asyncio.create_task(recv_task(client)),
                    ]
                    wait_reconnect_task = asyncio.create_task(self._reconnect_event.wait())

                    try:
                        done, _ = await asyncio.wait(
                            [asyncio.gather(*tasks), wait_reconnect_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )  # type: ignore
                        for task in done:
                            if task != wait_reconnect_task:
                                task.result()

                        if wait_reconnect_task not in done:
                            break

                        self._reconnect_event.clear()
                    finally:
                        await utils.aio.gracefully_cancel(*tasks, wait_reconnect_task)
            finally:
                if ws is not None:
                    await ws.close()

    async def _connect_ws(self) -> aiohttp.ClientWebSocketResponse:
        url = f"{self._connection_settings.url}"
        return await self._session.ws_connect(
            url,
            ssl=self._connection_settings.ssl_context,
            # headers=headers,
        )

    def _process_stream_event(self, data: dict, closing_ws: bool) -> None:
        message_type = data["type"]
        print(message_type)

        if message_type == ServerMessageType.RecognitionStarted:
            self._recognition_started.set()

        elif message_type == ServerMessageType.AddPartialTranscript:
            alts = live_transcription_to_speech_data(data)
            if len(alts) > 0 and alts[0].text:
                interim_event = stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=alts,
                )
                self._event_ch.send_nowait(interim_event)

        elif message_type == ServerMessageType.AddTranscript:
            alts = live_transcription_to_speech_data(data)
            if len(alts) > 0 and alts[0].text:
                final_event = stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=alts,
                )
                self._event_ch.send_nowait(final_event)

            if self._speech_duration > 0:
                usage_event = stt.SpeechEvent(
                    type=stt.SpeechEventType.RECOGNITION_USAGE,
                    alternatives=[],
                    recognition_usage=stt.RecognitionUsage(
                        audio_duration=self._speech_duration
                    ),
                )
                self._event_ch.send_nowait(usage_event)
                self._speech_duration = 0

        elif message_type == ServerMessageType.EndOfTranscript:
            if closing_ws:
                pass
            else:
                raise Exception("Speechmatics connection closed unexpectedly")


def live_transcription_to_speech_data(data: dict) -> List[stt.SpeechData]:
    speech_data: List[stt.SpeechData] = []

    start_time, end_time, is_eos = (
        data.get("start_time", 0),
        data.get("end_time", 0),
        data.get("is_eos", False),
    )

    content, language = (
        data.get("text").strip(),
        data.get("language", "en"),
    )

    # append punctuation to the previous result
   
    speech_data.append(
        stt.SpeechData(language, content, start_time=start_time, end_time=end_time)
    )

    return speech_data