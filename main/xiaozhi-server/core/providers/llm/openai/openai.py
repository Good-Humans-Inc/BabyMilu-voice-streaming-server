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
            is_active = True
            force_stateless = kwargs.get("stateless", self.stateless_default)
            conv_id = None if force_stateless else self.ensure_conversation(session_id)
            instructions = kwargs.get("instructions")
            extra_inputs = kwargs.get("extra_inputs", [])
            
            final_input = list(dialogue) + list(extra_inputs) if extra_inputs else dialogue
            
            with self.client.responses.stream(
                model=self.model_name,
                input=final_input,
                instructions=instructions,
                conversation=conv_id,
                store=bool(conv_id),
            ) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta":
                        delta = event.delta or ""
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
                                tail = delta[idx + len("</think>"):]
                                is_active = True
                                if tail:
                                    yield tail
                    elif event.type == "response.completed":
                        break

        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in response generation: {e}")

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        try:
            # Convert Chat Completions function schema to Responses tool schema
            tools = []
            if functions:
                for f in functions:
                    if f.get("type") == "function" and isinstance(f.get("function"), dict):
                        fn = f["function"]
                        tools.append({
                            "type": "function",
                            "name": fn["name"],
                            "description": fn.get("description", ""),
                            "parameters": fn.get("parameters", {}),
                        })
                    elif f.get("type") == "function" and "name" in f:
                        tools.append(f)

            def make_tool_delta(call_id, name, arguments):
                return [SimpleNamespace(
                    id=call_id,
                    function=SimpleNamespace(name=name, arguments=arguments)
                )]

            is_active = True
            force_stateless = kwargs.get("stateless", self.stateless_default)
            conv_id = None if force_stateless else self.ensure_conversation(session_id)
            instructions = kwargs.get("instructions")
            extra_inputs = kwargs.get("extra_inputs", [])
            
            final_input = list(dialogue) + list(extra_inputs) if extra_inputs else dialogue
            
            with self.client.responses.stream(
                model=self.model_name,
                input=final_input,
                tools=tools if tools else None,
                instructions=instructions,
                conversation=conv_id,
                store=bool(conv_id),
            ) as stream:
                function_call = {"id": None, "name": None, "arguments": ""}
                
                for event in stream:
                    event_type = event.type
                    
                    if event_type == "response.output_text.delta":
                        # Skip text deltas if we're collecting function call data
                        if function_call["id"]:
                            continue
                        
                        delta = event.delta or ""
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
                                tail = delta[idx + len("</think>"):]
                                is_active = True
                                if tail:
                                    yield tail, None
                    
                    elif event_type == "response.output_item.added":
                        # Detect function call initiation
                        if event.item.type == "function_call":
                            function_call["id"] = event.item.call_id
                            function_call["name"] = event.item.name
                            logger.bind(tag=TAG).info(
                                f"Function call started: {function_call['name']} "
                                f"(id={function_call['id']})"
                            )
                    
                    elif event_type == "response.function_call_arguments.delta":
                        # Accumulate function arguments
                        if event.delta:
                            function_call["arguments"] += event.delta
                    
                    elif event_type == "response.function_call_arguments.done":
                        # Function arguments complete
                        logger.bind(tag=TAG).info(
                            f"Function call arguments complete: {len(function_call['arguments'])} chars"
                        )
                    
                    elif event_type == "response.output_item.done":
                        # Output item completed - could be function call or text
                        pass
                    
                    elif event_type == "response.completed":
                        break

                # Emit consolidated function call if we collected one
                if function_call["id"] and function_call["name"]:
                    args = function_call["arguments"].strip()
                    if args:
                        try:
                            # Validate JSON
                            json.loads(args)
                            logger.bind(tag=TAG).info(
                                f"Emitting function call: {function_call['name']} "
                                f"with {len(args)} char args"
                            )
                            yield "", make_tool_delta(
                                function_call["id"],
                                function_call["name"],
                                args
                            )
                        except json.JSONDecodeError:
                            logger.bind(tag=TAG).warning(
                                f"Invalid JSON in function arguments: {args[:100]}"
                            )

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