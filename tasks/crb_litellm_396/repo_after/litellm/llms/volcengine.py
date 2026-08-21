from typing import List, Optional, Union

from litellm.llms.openai_like.chat.transformation import OpenAILikeChatConfig
from litellm.types.llms.openai import AllMessageValues


class VolcEngineConfig(OpenAILikeChatConfig):
    frequency_penalty: Optional[int] = None
    function_call: Optional[Union[str, dict]] = None
    functions: Optional[list] = None
    logit_bias: Optional[dict] = None
    max_tokens: Optional[int] = None
    n: Optional[int] = None
    presence_penalty: Optional[int] = None
    stop: Optional[Union[str, list]] = None
    temperature: Optional[int] = None
    top_p: Optional[int] = None
    response_format: Optional[dict] = None

    def __init__(
        self,
        frequency_penalty: Optional[int] = None,
        function_call: Optional[Union[str, dict]] = None,
        functions: Optional[list] = None,
        logit_bias: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        n: Optional[int] = None,
        presence_penalty: Optional[int] = None,
        stop: Optional[Union[str, list]] = None,
        temperature: Optional[int] = None,
        top_p: Optional[int] = None,
        response_format: Optional[dict] = None,
    ) -> None:
        locals_ = locals().copy()
        for key, value in locals_.items():
            if key != "self" and value is not None:
                setattr(self.__class__, key, value)

    @classmethod
    def get_config(cls):
        return super().get_config()

    def get_supported_openai_params(self, model: str) -> list:
        return [
            "frequency_penalty",
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "max_completion_tokens",
            "max_tokens",
            "n",
            "presence_penalty",
            "seed",
            "stop",
            "stream",
            "stream_options",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "function_call",
            "functions",
            "max_retries",
            "extra_headers",
            "thinking",
        ]  # works across all models

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
        replace_max_completion_tokens_with_max_tokens: bool = True,
    ) -> dict:
        optional_params = super().map_openai_params(
            non_default_params,
            optional_params,
            model,
            drop_params,
            replace_max_completion_tokens_with_max_tokens,
        )

        return optional_params

    @staticmethod
    def _move_thinking_to_extra_body(optional_params: dict) -> dict:
        if "thinking" not in optional_params:
            return optional_params

        optional_params = optional_params.copy()
        extra_body = optional_params.get("extra_body", {}).copy()
        extra_body["thinking"] = optional_params.pop("thinking")
        optional_params["extra_body"] = extra_body
        return optional_params

    def transform_request(
        self,
        model: str,
        messages: List[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        return super().transform_request(
            model=model,
            messages=messages,
            optional_params=self._move_thinking_to_extra_body(optional_params),
            litellm_params=litellm_params,
            headers=headers,
        )

    async def async_transform_request(
        self,
        model: str,
        messages: List[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        return await super().async_transform_request(
            model=model,
            messages=messages,
            optional_params=self._move_thinking_to_extra_body(optional_params),
            litellm_params=litellm_params,
            headers=headers,
        )
