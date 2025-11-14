import httpx
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
        # 增加timeout的配置项，单位为秒
        timeout = config.get("timeout", 300)
        self.timeout = int(timeout) if timeout else 300
        # Stateless default for this LLM instance (useful for memory LLM)
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

        logger.debug(
            f"意图识别参数初始化: {self.temperature}, {self.max_tokens}, {self.top_p}, {self.frequency_penalty}"
        )

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=httpx.Timeout(self.timeout))
        # Per-session conversation tracking
        self._conversations = {}

    def ensure_conversation(self, session_id):
        """Ensure and return an OpenAI conversation id for a given session_id."""
        try:
            state = self._conversations.get(session_id)
            if state and state.get("id"):
                return state["id"]
            conv = self.client.conversations.create()
            conv_id = getattr(conv, "id", None)
            if not conv_id:
                raise RuntimeError("Failed to create conversation id")
            self._conversations[session_id] = {"id": conv_id}
            return conv_id
        except Exception as e:
            logger.bind(tag=TAG).error(f"Create conversation failed: {e}")
            self._conversations[session_id] = {"id": None}
            return None

    def adopt_conversation_id_for_session(self, session_id, conversation_id):
        """Adopt an externally provided conversation id for this session."""
        try:
            if conversation_id:
                self._conversations[session_id] = {"id": conversation_id}
        except Exception:
            pass

    def has_conversation(self, session_id) -> bool:
        state = self._conversations.get(session_id)
        return bool(state and state.get("id"))

    def ensure_conversation_with_system(self, session_id, system_text: str):
        """Create conversation and seed a system message as the first item; return id."""
        try:
            state = self._conversations.get(session_id)
            if state and state.get("id"):
                return state["id"]
            if not system_text:
                return self.ensure_conversation(session_id)
            conv = self.client.conversations.create(
                items=[
                    {
                        "type": "message",
                        "role": "system",
                        "content": system_text,
                    }
                ]
            )
            conv_id = getattr(conv, "id", None)
            if not conv_id:
                raise RuntimeError("Failed to create conversation id")
            self._conversations[session_id] = {"id": conv_id}
            return conv_id
        except Exception as e:
            logger.bind(tag=TAG).error(f"Create conversation(with system) failed: {e}")
            return self.ensure_conversation(session_id)

    def response(self, session_id, dialogue, **kwargs):
        try:
            is_active = True
            force_stateless = kwargs.get("stateless", self.stateless_default)
            conv_id = None if force_stateless else self.ensure_conversation(session_id)
            instructions = kwargs.get("instructions")
            
            # Build stream parameters
            stream_params = {
                "model": self.model_name,
                "input": dialogue,
                "instructions": instructions,
                "conversation": conv_id if conv_id else None,
                "store": True if conv_id else False,
            }
            
            with self.client.responses.stream(**stream_params) as stream:
                for event in stream:
                    try:
                        etype = getattr(event, "type", None)
                        if etype == "response.output_text.delta":
                            delta = getattr(event, "delta", "") or ""
                            if not delta:
                                continue
                            if is_active:
                                if "<think>" in delta:
                                    idx = delta.find("<think>")
                                    head = delta[:idx]
                                    if head:
                                        yield head
                                    is_active = False
                                else:
                                    yield delta
                            else:
                                if "</think>" in delta:
                                    idx = delta.rfind("</think>")
                                    tail = delta[idx + len("</think>") :]
                                    is_active = True
                                    if tail:
                                        yield tail
                        elif etype == "response.completed":
                            break
                    except Exception:
                        continue

                try:
                    _ = stream.get_final_response()
                except Exception:
                    pass

        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in response generation: {e}")

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        try:
            # Convert Chat Completions function schema to Responses tool schema if needed
            resp_tools = []
            if isinstance(functions, list):
                for f in functions:
                    if isinstance(f, dict) and f.get("type") == "function" and isinstance(f.get("function"), dict):
                        fn = f.get("function", {})
                        resp_tools.append(
                            {
                                "type": "function",
                                "name": fn.get("name"),
                                "description": fn.get("description", ""),
                                "parameters": fn.get("parameters", {}),
                            }
                        )
                    elif isinstance(f, dict) and f.get("type") == "function" and ("name" in f):
                        resp_tools.append(f)

            calls_state = {}

            def make_tool_delta(call_id, name, args_delta):
                tool_obj = SimpleNamespace(
                    id=call_id,
                    function=SimpleNamespace(name=name or "", arguments=args_delta or ""),
                )
                return [tool_obj]

            is_active = True
            force_stateless = kwargs.get("stateless", self.stateless_default)
            conv_id = None if force_stateless else self.ensure_conversation(session_id)
            instructions = kwargs.get("instructions")
            
            with self.client.responses.stream(
                model=self.model_name,
                input=dialogue,
                tools=resp_tools if resp_tools else None,
                instructions=instructions,
                conversation=conv_id if conv_id else None,
                store=True if conv_id else False,
            ) as stream:
                for event in stream:
                    try:
                        etype = getattr(event, "type", None)
                        if etype == "response.output_text.delta":
                            delta = getattr(event, "delta", "") or ""
                            if not delta:
                                continue
                            if is_active:
                                if "<think>" in delta:
                                    idx = delta.find("<think>")
                                    head = delta[:idx]
                                    if head:
                                        yield head, None
                                    is_active = False
                                else:
                                    yield delta, None
                            else:
                                if "</think>" in delta:
                                    idx = delta.rfind("</think>")
                                    tail = delta[idx + len("</think>") :]
                                    is_active = True
                                    if tail:
                                        yield tail, None
                        elif etype and "function_call" in etype:
                            call_id = getattr(event, "call_id", None) or getattr(event, "id", None)
                            name = getattr(event, "name", None)
                            args_delta = getattr(event, "arguments", None) or getattr(event, "delta", None)
                            if not call_id:
                                call_id = f"call_{hash(name) & 0xFFFFFFFF:x}"
                            state = calls_state.setdefault(call_id, {"name": name, "args": ""})
                            if name and not state.get("name"):
                                state["name"] = name
                            if args_delta:
                                state["args"] += str(args_delta)
                                yield "", make_tool_delta(call_id, state["name"], str(args_delta))
                        elif etype == "response.completed":
                            break
                    except Exception:
                        continue

                try:
                    for cid, st in calls_state.items():
                        if st.get("args"):
                            yield "", make_tool_delta(cid, st.get("name"), st.get("args"))
                    _ = stream.get_final_response()
                except Exception:
                    pass

        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in function call streaming: {e}")
            yield f"【OpenAI服务响应异常: {e}】", None

    def response_with_structured_output(self, dialogue, structured_output, **kwargs):
        try:
            completion = self.client.chat.completions.create(
                model=self.model_name,
                messages=dialogue,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format=structured_output
            )
            
            response_text = completion.choices[0].message.content
            return response_text
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in structured output streaming: {e}")
            return f"【OpenAI服务响应异常: {e}】"