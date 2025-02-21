from __future__ import annotations

from typing import Literal, Union

from livekit.agents import (
    APIConnectionError,
    llm,
)
from livekit.agents.llm import ToolChoice
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

import logging

logger = logging.getLogger("livekit.plugins.kktensor")


class LLM(llm.LLM):
    def __init__(
        self,
        # chat_engine: BaseChatEngine,
    ) -> None:
        super().__init__()
        # self._chat_engine = chat_engine

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        fnc_ctx: llm.FunctionContext | None = None,
        temperature: float | None = None,
        n: int | None = 1,
        parallel_tool_calls: bool | None = None,
        tool_choice: (
            Union[ToolChoice, Literal["auto", "required", "none"]] | None
        ) = None,
    ) -> "LLMStream":
        if fnc_ctx is not None:
            logger.warning("fnc_ctx is currently not supported with kktensor.LLM")

        return LLMStream(
            self,
            chat_ctx=chat_ctx,
            conn_options=conn_options,
        )


class LLMStream(llm.LLMStream):
    def __init__(
        self,
        llm: LLM,
        *,
        chat_ctx: llm.ChatContext,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(
            llm, fnc_ctx=None, conn_options=conn_options, chat_ctx=chat_ctx
        )

    async def _metrics_task(self, *args, **kwargs):
        # TODO: Implement metrics collection for kktensor.LLM
        pass

    async def _run(self) -> None:
        chat_ctx = self._chat_ctx.copy()
        user_msg = chat_ctx.messages.pop()

        if user_msg.role != "user":
            raise ValueError(
                "The last message in the chat context must be from the user"
            )

        assert isinstance(
            user_msg.content, str
        ), "user message content must be a string"

        try:
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    request_id="",
                    choices=[
                        llm.Choice(
                            delta=llm.ChoiceDelta(
                                role="assistant",
                                content=user_msg.content,
                            )
                        )
                    ],
                )
            )
        except Exception as e:
            raise APIConnectionError() from e

