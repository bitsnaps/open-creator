from langchain.tools import BaseTool, format_tool_to_openai_function
from langchain.callbacks.manager import CallbackManagerForToolRun
from pydantic import BaseModel, Field
from creator.utils import remove_title, split_code_blocks
from typing import Type, Optional
import threading
import traceback
import ast
from loguru import logger
import io
import sys


logger.add("tmp.log")


class PythonInput(BaseModel):
    code: str = Field(description="The code to execute")


class SafePythonInterpreter(BaseTool):
    name: str = "python"
    description: str = "A python interpreter for safe run"
    args_schema: Type[BaseModel] = PythonInput
    namespace: dict = {}
    setup_done: bool = False

    ALLOWED_FUNCTIONS = {"create", "save", "search", "CodeSkill"}
    ALLOW_METHODS = {".show", ".test", ".run", "__add__", "__gt__", "__lt__", "__annotations__"}
    TIMEOUT = 1200

    def setup(self, setup_code: str):
        self.run_code(setup_code)
        self.setup_done = True

    def is_allowed_function(self, node):
        # Check if the node represents a call to an allowed function
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in self.ALLOWED_FUNCTIONS
        )

    def is_allowed_method(self, node):
        # Check if the node represents a call to an allowed method of an allowed class
        return (
            isinstance(node, ast.Call)
            and any( allowed_method in ast.unparse(node.func) for allowed_method in self.ALLOW_METHODS)
        )

    def preprocess(self, query: str):
        try:
            # Parse the code to an AST
            tree = ast.parse(query)

            # If setup is done, restrict the allowed nodes
            if getattr(self, "setup_done", False):
                for node in ast.walk(tree):
                    # Check for unsafe nodes
                    if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
                        raise ValueError(f"Usage of {node.__class__.__name__} nodes is not allowed")
                    # Check for disallowed function/method calls
                    elif isinstance(node, ast.Call):
                        if not (self.is_allowed_function(node) or self.is_allowed_method(node)):
                            raise ValueError("Usage of disallowed function/method: " + ast.unparse(node))

            # If all checks pass, return the original query
            return query

        except Exception as e:
            # Save exception info in the namespace to retrieve it later in the main thread.
            self.namespace['_preprocess_info'] = (type(e), e, e.__traceback__)
            return ""
        
    def is_expression(self, code: str) -> bool:
        try:
            compile(code, "", "eval")
            return True
        except SyntaxError:
            return False

    def execute_last_line(self, last_line):
        output_io = io.StringIO()
        sys.stdout = output_io  # Redirect stdout to capture print statements
        output = ""
        if self.is_expression(last_line):
            eval_output = eval(last_line, self.namespace)
            if eval_output is not None:
                output += str(eval_output)
        else:
            exec(last_line, self.namespace)
        printed_output = output_io.getvalue()
        if printed_output:
            output += printed_output
            output_io.seek(0)
            output_io.truncate(0)
        return output
    
    def execute_code_blocks(self, blocks):
        output_io = io.StringIO()
        sys.stdout = output_io
        output = ""
        for block in blocks:
            exec(block, self.namespace)
        printed_output = output_io.getvalue()
        if printed_output:
            output += printed_output
            output_io.seek(0)
            output_io.truncate(0)
        return output
    
    def run_code(self, query: str) -> dict:
        output = ""
        code_blocks = split_code_blocks(query)
        last_line = code_blocks.pop(-1) if len(code_blocks) > 0 else ""

        try:
            if len(code_blocks) > 0:
                output += self.execute_code_blocks(code_blocks)
            if last_line:
                output += self.execute_last_line(last_line)
            
            self.namespace.pop("_stdout_info", None)
            if output:
                self.namespace['_stdout_info'] = output
        except Exception as e:
            # Save exception info in the namespace to retrieve it later in the main thread.
            self.namespace['_exec_info'] = (type(e), e, e.__traceback__)
        finally:
            sys.stdout = sys.__stdout__

    def run_with_return(self, query: str) -> dict[str, str]:
        # Run the code in a separate thread and wait for it to finish or to timeout.
        thread = threading.Thread(target=self.run_code, args=(query,))
        thread.start()
        thread.join(timeout=self.TIMEOUT)

        stdout = self.namespace.get('_stdout_info', "")
        # If the thread is still alive after the timeout, it is stuck in a long-running operation (e.g., an infinite loop).
        if thread.is_alive():
            # If possible, kill the thread. Note: This is not always safe and may result in inconsistencies.
            # ... add any thread termination logic here ...
            return {"status": "error", "stdout": stdout, "stderr": "Code execution timed out"}

        # Retrieve and clear the saved exception info from the namespace.
        exec_info = self.namespace.pop('_exec_info', None)
        if exec_info:
            # Format and return the exception info.
            tb_lines = traceback.format_exception(*exec_info)
            return {"status": "error", "stdout": stdout, "stderr": "".join(tb_lines)}

        return {"status": "success", "stdout": stdout, "stderr": ""}

    def _run(self,
             query: str,
             run_manager: Optional[CallbackManagerForToolRun] = None
             ) -> dict[str, str]:
        # Preprocess the query
        query = self.preprocess(query)

        # If there was an error in preprocessing, retrieve and return the exception info.
        preprocess_info = self.namespace.pop('_preprocess_info', None)

        if preprocess_info:
            tb_lines = traceback.format_exception(*preprocess_info)
            return {"status": "error", "stdout": "", "stderr": "".join(tb_lines)}

        return self.run_with_return(query)

    def to_function_schema(self):
        function_schema = format_tool_to_openai_function(self)
        function_schema["parameters"] = remove_title(function_schema["parameters"])
        return function_schema
