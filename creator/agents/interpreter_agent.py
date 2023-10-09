from typing import List, Dict, Any, Optional
import json

from langchain.output_parsers.openai_functions import JsonOutputFunctionsParser
from langchain.schema.messages import FunctionMessage, HumanMessage
from langchain.prompts import ChatPromptTemplate
from langchain.adapters.openai import convert_message_to_dict, convert_openai_messages
from langchain.chains import LLMChain
from langchain.callbacks.manager import CallbackManager
from langchain.tools.base import BaseTool
from langchain.output_parsers.json import parse_partial_json

from creator.code_interpreter import CodeInterpreter
from creator.config.library import config
from creator.utils import truncate_output, ask_run_code_confirm, get_user_info, load_system_prompt

from creator.llm.llm_creator import create_llm


# prompt modified from: https://github.com/KillianLucas/open-interpreter/blob/11200b25de773b78a63874a8378872eaec39abc7/interpreter/config.yaml#L1
# MIT license
_SYSTEM_TEMPLATE = load_system_prompt(config.interpreter_agent_prompt_path)
DEBUGGING_TIPS = load_system_prompt(config.tips_for_debugging_prompt_path)


def fix_run_python(function_call):
    name = function_call.get("name", "run_code")
    arguments = function_call.get("arguments", "{}")
    arguments_json = parse_partial_json(arguments)
    if name != "run_code" or not arguments_json:
        return {
            "name": "run_code",
            "arguments": json.dumps({"language": "python", "code": arguments}, ensure_ascii=False)
        }
    return function_call


class CodeInterpreterAgent(LLMChain):
    total_tries: int = 10
    tool: BaseTool

    @property
    def _chain_type(self):
        return "CodeInterpreterAgent"

    @property
    def input_keys(self) -> List[str]:
        return ["messages"]

    def _call(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[CallbackManager] = None,
    ) -> Dict[str, Any]:

        messages = inputs.pop("messages")
        allow_user_confirm = config.run_human_confirm
        langchain_messages = convert_openai_messages(messages)

        total_tries = self.total_tries
        current_try = 0

        llm_with_functions = self.llm.bind(functions=[self.tool.to_function_schema()])

        callback = None
        if self.llm.callbacks is not None:
            callback = self.llm.callbacks.handlers[0]

        while current_try < total_tries:
            if callback:
                callback.on_chain_start()

            prompt = ChatPromptTemplate.from_messages(messages=[
                ("system", _SYSTEM_TEMPLATE + get_user_info()),
                *langchain_messages
            ])
            llm_chain = prompt | llm_with_functions
            message = llm_chain.invoke(inputs)
            langchain_messages.append(message)
            function_call = message.additional_kwargs.get("function_call", None)
            if function_call is None:
                break

            can_run_code = True
            if allow_user_confirm:
                can_run_code = ask_run_code_confirm()
            if not can_run_code:
                break
            function_call = fix_run_python(function_call)
            message.additional_kwargs["function_call"] = function_call
            langchain_messages[-1] = message
            arguments = parse_partial_json(function_call.get("arguments", "{}"))
            tool_result = self.tool.run(arguments)
            tool_result = truncate_output(tool_result)
            output = str(tool_result.get("stdout", "")) + str(tool_result.get("stderr", ""))
            if callback:
                callback.on_tool_end(output)

            function_message = FunctionMessage(name="run_code", content=json.dumps(tool_result, ensure_ascii=False))
            langchain_messages.append(function_message)
            if len(tool_result.get("stderr", "")) > 0 and "error" in tool_result["stderr"].lower():  # add tips for debugging
                langchain_messages.append(HumanMessage(content=DEBUGGING_TIPS))
            elif len(output) > 100:  # tips for avoiding repeating the output of `run_code`
                langchain_messages.append(HumanMessage(content="go on to next step if has, otherwise end."))
            current_try += 1
            if callback:
                callback.on_chain_end()

        openai_message = list(map(convert_message_to_dict, langchain_messages))
        if callback:
            callback.message_box.end()
        return {
            "messages": openai_message
        }


def create_code_interpreter_agent(llm):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _SYSTEM_TEMPLATE),
        ]
    )
    tool = CodeInterpreter()
    function_schema = tool.to_function_schema()
    llm_kwargs = {"functions": [function_schema], "function_call": {"name": function_schema["name"]}}
    chain = CodeInterpreterAgent(
        llm=llm,
        prompt=prompt,
        llm_kwargs=llm_kwargs,
        output_parser=JsonOutputFunctionsParser(),
        output_key="messages",
        tool=tool,
        verbose=False,
    )
    return chain


llm = create_llm(temperature=config.temperature, model=config.model, streaming=config.use_stream_callback, verbose=True)
code_interpreter_agent = create_code_interpreter_agent(llm=llm)
