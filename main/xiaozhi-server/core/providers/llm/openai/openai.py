import httpx
import json
import openai
from types import SimpleNamespace
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")
        
        timeout = config.get("timeout", 300)
        self.timeout = int(timeout) if timeout else 300
        self.stateless_default = bool(config.get("stateless", False))

        param_defaults = {
            "max_tokens": (500, int),
            "temperature": (0.7, lambda x: round(float(x), 1)),
            "top_p": (1.0, lambda x: round(float(x), 1)),
            "frequency_penalty": (0, lambda x: round(float(x), 1)),
        }

        for param, (default, converter) in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else default,
                )
            except (ValueError, TypeError):
                setattr(self, param, default)

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        
        self.client = openai.OpenAI(
            api_key=self.api_key, 
            base_url=self.base_url, 
            timeout=httpx.Timeout(self.timeout)
        )
        self._conversations = {}

    def ensure_conversation(self, session_id):
        """Ensure and return an OpenAI conversation id for a given session_id."""
        state = self._conversations.get(session_id)
        if state and state.get("id"):
            return state["id"]
        
        try:
            conv = self.client.conversations.create()
            conv_id = conv.id
            self._conversations[session_id] = {"id": conv_id}
            return conv_id
        except Exception as e:
            logger.bind(tag=TAG).error(f"Create conversation failed: {e}")
            return None

    def has_conversation(self, session_id) -> bool:
        state = self._conversations.get(session_id)
        return bool(state and state.get("id"))

    def adopt_conversation_id_for_session(self, session_id, conversation_id):
        """Adopt an externally provided conversation ID (e.g., from Firestore) for this session."""
        if conversation_id:
            self._conversations[session_id] = {"id": conversation_id}
            logger.bind(tag=TAG).info(
                f"Adopted conversation {conversation_id} for session {session_id}"
            )

    def ensure_conversation_with_system(self, session_id, system_text: str):
        """Create conversation and seed a system message as the first item."""
        state = self._conversations.get(session_id)
        if state and state.get("id"):
            return state["id"]
        
        if not system_text:
            return self.ensure_conversation(session_id)
        
        try:
            conv = self.client.conversations.create(
                items=[
                    {
                        "type": "message",
                        "role": "system",
                        "content": system_text,
                    }
                ]
            )
            conv_id = conv.id
            self._conversations[session_id] = {"id": conv_id}
            return conv_id
        except Exception as e:
            logger.bind(tag=TAG).error(f"Create conversation with system failed: {e}")
            return self.ensure_conversation(session_id)

    def response(self, session_id, dialogue, **kwargs):
        try:
            # Normalize dialogue to ensure all messages have content field
            for msg in dialogue:
                if "role" in msg and "content" not in msg:
                    msg["content"] = ""
            
            # Use Chat Completions API (compatible with ChatGLM)
            responses = self.client.chat.completions.create(
                model=self.model_name,
                messages=dialogue,
                stream=True,
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
                temperature=kwargs.get("temperature", self.temperature),
                top_p=kwargs.get("top_p", self.top_p),
                frequency_penalty=kwargs.get(
                    "frequency_penalty", self.frequency_penalty
                ),
            )

            is_active = True
            chunk_count = 0
            total_content_length = 0
            for chunk in responses:
                chunk_count += 1
                try:
                    delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
                    content = getattr(delta, "content", "") if delta else ""
                except IndexError:
                    content = ""
                if content:
                    total_content_length += len(content)
                    if "<think>" in content:
                        is_active = False
                        content = content.split("<think>")[0]
                    if "</think>" in content:
                        is_active = True
                        content = content.split("</think>")[-1]
                    if is_active:
                        yield content
            
            logger.bind(tag=TAG).info(f"LLM streaming complete: {chunk_count} chunks, {total_content_length} chars total")

        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in response generation: {e}")
            import traceback
            logger.bind(tag=TAG).error(f"Traceback: {traceback.format_exc()}")

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        try:
            # Normalize dialogue to ensure all messages have content field
            for msg in dialogue:
                if "role" in msg and "content" not in msg:
                    msg["content"] = ""
            
            # Use Chat Completions API with tools (compatible with ChatGLM)
            stream = self.client.chat.completions.create(
                model=self.model_name,
                messages=dialogue,
                stream=True,
                tools=functions,
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
                temperature=kwargs.get("temperature", self.temperature),
                top_p=kwargs.get("top_p", self.top_p),
                frequency_penalty=kwargs.get(
                    "frequency_penalty", self.frequency_penalty
                ),
            )

            is_active = True
            chunk_count = 0
            total_content_length = 0
            for chunk in stream:
                chunk_count += 1
                try:
                    if not getattr(chunk, "choices", None):
                        continue
                    
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", "")
                    tool_calls = getattr(delta, "tool_calls", None)
                    
                    # Handle tool calls
                    if tool_calls:
                        yield content, tool_calls
                        continue
                    
                    # Handle text content
                    if content:
                        total_content_length += len(content)
                        if "<think>" in content:
                            is_active = False
                            content = content.split("<think>")[0]
                        if "</think>" in content:
                            is_active = True
                            content = content.split("</think>")[-1]
                        if is_active and content:
                            yield content, None
                    else:
                        yield "", None
                        
                except IndexError:
                    yield "", None
            
            logger.bind(tag=TAG).info(f"LLM function call streaming complete: {chunk_count} chunks, {total_content_length} chars total")

        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in function call streaming: {e}")
            import traceback
            logger.bind(tag=TAG).error(f"Traceback: {traceback.format_exc()}")
            yield f"【OpenAI服务响应异常: {e}】", None
