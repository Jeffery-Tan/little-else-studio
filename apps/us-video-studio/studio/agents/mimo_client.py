"""Official MiMo Chat Completions, checked against Xiaomi docs on 2026-10-06.

Shares validated thinking/tool framing and cancellation with DeepSeek, but
uses MiMo's own endpoint, completion limit parameter and private replay label.
No endpoint fallback, retries, web search or public reasoning are enabled.
"""
from copy import copy

from .client import DEFAULT_MAX_TOKENS, ModelError
from .deepseek_client import DeepSeekClient

BASE_URL = 'https://api.xiaomimimo.com/v1'
ENDPOINT = BASE_URL + '/chat/completions'
MODELS = ('mimo-v2.6-flash',)
REASONING_BLOCK = 'mimo_reasoning'


class MiMoClient(DeepSeekClient):
    allow_video = True
    reject_nested_empty_strict_objects = False
    provider_name = 'MiMo'
    reasoning_block = REASONING_BLOCK

    def __init__(self, token, *, max_tokens=DEFAULT_MAX_TOKENS, timeout=180.0,
                 check_cancel=None, cancel_event=None, thinking=True):
        self.json_output = False
        self.structured_observation = False
        if type(thinking) is not bool:
            raise ValueError('MiMo thinking must be boolean')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 131072:
            raise ValueError('MiMo max_completion_tokens must be an integer between 1 and 131072')
        try:
            super().__init__(token, max_tokens=max_tokens, timeout=timeout,
                             check_cancel=check_cancel, cancel_event=cancel_event,
                             reasoning_effort='high' if thinking else 'none')
        except ValueError:
            raise ValueError('MiMo API key must contain ASCII characters without whitespace or control characters') from None

    def with_json_output(self):
        """A separate first-request JSON observer, without changing role clients."""
        observer = copy(self)
        observer.json_output = True
        observer.structured_observation = False
        return observer

    def with_structured_observation(self):
        """An independent strict data observer; role clients stay unchanged."""
        observer = copy(self)
        observer.json_output = False
        observer.structured_observation = True
        return observer

    @staticmethod
    def _endpoint(payload):
        return ENDPOINT

    def _payload(self, model, system, messages, tools):
        if self.structured_observation:
            from ..image_understanding import is_structured_report_tools
            if not is_structured_report_tools(tools):
                raise ModelError(0, 'MiMo image observer requires its exact single data report schema',
                                 provider_invoked=False, retryable=False)
        if self.json_output and tools:
            raise ModelError(0, "MiMo observation JSON mode cannot execute function tools",
                             provider_invoked=False, retryable=False)
        payload = super()._payload(model, system, messages, tools)
        if self.json_output:
            payload["response_format"] = {"type": "json_object"}
        if self.structured_observation:
            payload['tool_choice'] = 'auto'
        payload['max_completion_tokens'] = payload.pop('max_tokens')
        # MiMo Chat Completions documents thinking.type, not reasoning_effort.
        payload.pop('reasoning_effort', None)
        return payload
