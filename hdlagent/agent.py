import importlib
import markdown
import openai
import os
import octoai
import re
import subprocess
import sys
import time
import yaml

from importlib import resources

def list_openai_models(warn: bool = True):
    if "OPENAI_API_KEY" in os.environ:
        ret_list = []
        for entry in openai.Model.list()['data']:
            ret_list.append(entry['id'])
        return ret_list
    else:
        if warn:
            print("OPENAI_API_KEY not set")
        return []

def list_octoai_models(warn: bool = True):
    if "OCTOAI_TOKEN" in os.environ:
        return octoai.chat.get_model_list()
    else:
        if warn:
            print("OCTOAI_TOKEN not set")
        return []

class Agent:
    def __init__(self, spath: str, model: str, lang: str, use_init_context:bool = False, use_supp_context: bool = False, use_spec: bool = False):

        self.script_dir = spath
        # self.script_dir = os.path.dirname(os.path.abspath(__file__))
        # Common responses
        with open(os.path.join(self.script_dir,"common/common.yaml"), 'r') as file:
            common = yaml.safe_load(file)

        # Set by Lang
        with open(os.path.join(self.script_dir, lang, lang + ".yaml"), 'r') as file:
            config = yaml.safe_load(file)

        # Default prefixes and suffixes for queries
        self.responses = config['responses']
        self.responses.update(common['responses'])
        
        # Pre-loads long initial language context from file(s)
        self.used_init_context = use_init_context
        self.initial_contexts  = []
        if use_init_context:
            for context in config['initial_contexts']:
                with open(os.path.join(self.script_dir, lang, context), 'r') as file:
                    self.initial_contexts.append({'role': 'user','content': file.read()})
        
        # Supplemental contexts are loaded from yaml file, dictionary of {error: suggestion} pairs
        self.used_supp_context     = use_supp_context
        self.supplemental_contexts = {}
        if use_supp_context:
            self.supplemental_contexts = config['supplemental_contexts']

        # Compilation script for the target language
        self.compile_script = config['compile_script'] + " "

        # File extension of the target language
        self.file_ext = config['file_extension']

        # Determine the root directory of the project
        current_script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_script_dir)
        sys.path.append(project_root)
        # Error message check from compiler for target language
        module_name, func_name = config['error_check_function'].rsplit('.', 1)
        module = importlib.import_module(module_name)
        self.check_errors = getattr(module, func_name)

        # Formats compiled verilog into style that allows for Yosys' Logical Equivalence Checks
        module_name, func_name = config['reformat_verilog_function'].rsplit('.', 1)
        module = importlib.import_module(module_name)
        self.reformat_verilog = getattr(module, func_name)

        # Name to be specified in chat completion API of target LLM
        self.octoai_model = False
        self.openai_model = False
        if model in list_octoai_models(False):
            self.octoai_model = True
        elif model in list_openai_models(False):
            self.openai_model = True
        else:
            print("Error: invalid model, please check --list_openai_models or --list_octoai_models for available LLM selection, exiting...")
            exit()
        self.model = model

        # Resources used as a part of conversational flow and performance counters
        self.spec_conversation    = []
        self.compile_conversation = []
        self.name                 = ""
        self.pipe_stages          = 0
        self.lec_script           = os.path.join(self.script_dir,"common/comb_lec")
        self.tb_script            = os.path.join(self.script_dir,"common/iverilog_tb")
        self.comp_n               = 0
        self.comp_f               = 0
        self.lec_n                = 0
        self.lec_f                = 0
        self.prompt_tokens        = 0
        self.completion_tokens    = 0
        self.llm_query_time       = 0.0
        self.world_clock_time     = 0.0

        # Where the resulting source files and logs will be managed and left
        self.w_dir       = './'
        self.spec_log    = "./logs/spec_log.md"
        self.compile_log = "./logs/compile_log.md"
        self.fail_log    = "./logs/fail.md"
        if use_spec:
            self.spec    = "spec.yaml"
        else:
            self.spec = None
        self.code        = "out" + self.file_ext
        self.verilog     = "out.v"
        self.tb          = "./tests/tb.v"

        # File path to golden verilog module used for Logical Equivalence Check
        self.gold = None

        # List of required inputs and outputs for module declaration
        self.io = []
        

    # The current run will assume this is the name of the target module
    # and will name files, perform queries, and define modules accordingly.
    #
    # Intended use: at the beginning of a new initial prompt submission
    def set_module_name(self, name: str):
        self.name = name
        self.update_paths()

    # The current run will assume this is the amount of pipeline stages
    # desired for the prompted RTL design, this will modify the LEC
    # method and queries accordingly.
    #
    # Intended use: at the beginning of a new initial propmt submission
    def set_pipeline_stages(self, pipe_stages: int):
        if (pipe_stages > 0):
            self.lec_script = os.path.join(self.script_dir,"common/temp_lec")
        else:
            self.lec_script = os.path.join(self.script_dir,"common/comb_lec")
        self.pipe_stages = pipe_stages

    # Creates and registers the 'working directory' for the current run as
    # so all output from the run including logs and produced source code
    # are left in this directory when the run is complete
    #
    # Intended use: right after the beginning of a new Agent's instantiation
    def set_w_dir(self, path: str):
        # Now create the dir if it does not exist
        directory = os.path.dirname(self.w_dir)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
        self.update_paths()

    # Helper function to others that may change or set file names and paths.
    # Keeps all file pointers up to date
    #
    # Intended use: inside of 'set_w_dir' or 'set_module_name'
    def update_paths(self):
        self.compile_log  = os.path.join(self.w_dir, "logs", self.name + "_compile_log.md")
        self.fail_log     = os.path.join(self.w_dir, "logs", self.name + "_fail.md")
        if self.spec is not None:
            self.spec     = os.path.join(self.w_dir, self.name + "_spec.yaml")
            self.spec_log = os.path.join(self.w_dir, "logs", self.name + "_spec_log.md")
        self.code         = os.path.join(self.w_dir, self.name + self.file_ext)
        self.verilog      = os.path.join(self.w_dir, self.name + ".v")
        self.tb           = os.path.join(self.w_dir, "tests", self.name + "_tb.v")

    # Helper function to check if the spec exists or not
    #
    # Intended use: when deciding whether to use the contents of a spec
    def spec_exists(self):
        return os.path.exists(self.spec)

    # Helper function that extracts and reformats information from spec yaml file
    # to create initial prompt(s) for compilation loop and testbench
    # loop (if applicable).
    #
    # Intended use: before a lec loop is started so the prompt can be reformatted and formalized
    def read_spec(self):
        if self.spec is None:
            print("Error: attempting to read spec in a run where it is not employed, exiting...")
            exit()
        with open(self.spec, 'r') as file:
            spec = yaml.safe_load(file)
        file.close()
        description = spec['description']
        interface   = spec['interface']
        return (self.responses['spec_to_prompt']).format(description=description,interface=interface)
        
    # Helper function to create a triplet list that defines a Verilog modules io
    # after being exctracted in a linted order from Yosys 'write_verilog' cmd
    #
    # Intended use: to set self.io
    def extract_io(self, wires: str):
        lines = [line.strip() for line in wires.split('\n') if line.strip()]

        def format_line(line):
            parts = line.split()
            if not parts[1].startswith('['):
                parts.insert(1, '[0:0]')
            parts[-1] = parts[-1].replace(';', '')
            return tuple(parts)

        return [format_line(line) for line in lines]


    # Registers the file path of the golden Verilog to be used for LEC
    # and dumps the contents into the file.
    #
    # Intended use: at the beginning of a new initial prompt submission
    def dump_gold(self, contents: str):
        verilog_lines = contents.split('\n')
        gold_contents = ""
        for line in verilog_lines:
            gold_contents += line.rstrip()
            gold_contents += "\n"
        gold_contents += "\n"
        self.gold      = os.path.join(self.w_dir, "tests", self.name + "_gold.v")
        os.makedirs(os.path.dirname(self.gold), exist_ok=True)
        with open (self.gold, "w") as file:
            file.write(gold_contents)
        self.check_gold(self.gold)
        
    # Helper function to prevent API charges in case gold Verilog
    # supplied is not valid or the file path is incorrect.
    #
    # Intended use: only when registering a gold module path
    def check_gold(self, file_path: str):
        # Helps users identify if their "golden" model has issues
        if not os.path.exists(file_path):
            print("Error: supplied Verilog file path {} invalid, exiting...".format(file_path))
            exit()
        script = os.path.join(self.script_dir,"common/check_verilog") + " " + file_path # better way to do this?
        res    = subprocess.run([script], capture_output=True, shell=True)
        res_string = (str(res.stdout))[2:-1].replace("\\n", "\n")
        if "SUCCESS" not in res_string:
            print("Error: supplied Verilog file path {} did not pass Yosys compilation check, fix the following errors/warnings and try again".format(file_path))
            print(res_string)
            print("exiting...")
            exit()
        else:   # populate the io list
            lines = res_string.split('\n')
            # Remove the 'SUCCESS' line
            res_string = '\n'.join(lines[1:])
            self.io = self.extract_io(res_string)
            #print(self.io)

    # Parses common.yaml file for the 'request_spec' strings to create the
    # contents of the 'spec initial instruction' tuple which are the 2 queries
    # given to the LLM that requests a spec be generated given an informal
    # request for the design.
    #
    # Intended use: to begin the spec generation process
    def get_spec_initial_instructions(self, prompt: str):
        description = (self.responses['request_spec_description']).format(prompt=prompt)
        interface   = self.responses['request_spec_interface']
        return (description, interface)

    # Parses language-specific *.yaml file for the 'give_instr' strings
    # to create the 'compile initial instruction' which is the first query given
    # to the LLM that specifies the desired behavior, name, and pipeline stages
    # of the RTL. This query is preceeded by the Initial Contexts
    # "I want a circuit that does this <def>, named <name>, write the RTL"
    #
    # Intended use: to begin the iterative part of the compilation conversation
    def get_compile_initial_instruction(self, prompt: str): 
        name        = self.name
        pipe_stages = self.pipe_stages
        prefix = self.responses['comb_give_instr_prefix']
        output_count = 0    # XXX - fixme hacky, Special case for DSLX
        for wire in self.io:
            if wire[0] == "output":
                output_count += 1
        if (output_count == 1) and ('simple_give_instr_suffix' in self.responses):
            suffix = self.responses['simple_give_instr_suffix']
        else:
            suffix = self.responses['give_instr_suffix']
        if pipe_stages > 0:
            prefix = self.responses['pipe_give_instr_prefix']
        #if self.spec is not None:   # Spec run, read from *.yaml file
        #    spec = self.read_spec()
        #    prompt = spec['description'] + self.responses['spec_give_interface'] + spec['interface']
        return (prefix + prompt + suffix).format(name=name, pipe_stages=pipe_stages)

    # Parses language-specific *.yaml file for the 'compile_err' strings
    # to create the 'iteration instruction' which is the query sent to the
    # LLM after a compilation error was detected from its previous response.
    # "I got this compiler error: <err_mesg>, fix it"
    #
    # Intended use: inside compile conversation loop, after get_initial_instruction()
    def get_compile_iteration_instruction(self, compiler_output: str):
        clarification = self.responses['compile_err_prefix'] + compiler_output
        if self.used_supp_context:
            clarification += self.suggest_fix(compiler_output)
        return clarification

    # Parses language-specific *.yaml file for the 'lec_fail' strings
    # to create the 'lec fail instruction' which is the query sent to the
    # LLM after Yosys Logical Equivalence Check failed versus the golden model..
    # "Comparing your code to desired truth table here are mismatches: <lec_mesg>, fix it"
    #
    # Intended use: inside lec loop, after lec failure detected and more lec iterations available
    def get_lec_fail_instruction(self, lec_output: str):
        lec_fail_prefix = self.responses['comb_lec_fail_prefix']
        lec_fail_suffix = self.responses['comb_lec_fail_suffix']
        if self.pipe_stages > 0:
            lec_fail_prefix = self.responses['pipe_lec_fail_prefix']
            lec_fail_suffix = self.responses['pipe_lec_fail_suffix']
        return lec_fail_prefix + lec_output + lec_fail_suffix

    # Primary interface between supported LLM's API and the Agent. 
    # Send queries and returns their response(s), all while maintaining
    # the context by appending both query and response to the context history,
    # managed performance counters for token usage, time, etc...
    #
    # Intended use: primary interface with LLM
    def query_model(self, conversation, clarification: str, compile_convo: bool = False):
        # Add a clarification message, continuing the conversation
        conversation.append({
            'role': 'user',
            'content': clarification
        })
        print("**User:**\n" + clarification)
        query_start_time = time.time()

        if self.octoai_model:
            client = octoai.client.Client()
            completion = client.chat.completions.create(
                model=self.model,
                messages=conversation,
                max_tokens=8192,
                presence_penalty=0,
                temperature=0.1,
                top_p=0.9,
            )
            response = completion.dict()['choices'][0]['message']['content']
            self.prompt_tokens += completion.dict()['usage']['prompt_tokens']
            self.completion_tokens += completion.dict()['usage']['completion_tokens']
        elif self.openai_model:
            # Use the OpenAI Completion API in a chat-based approach
            completion = openai.ChatCompletion.create(
                model=self.model,  # Use the model we want for research
                messages=conversation,
            )
            response = completion.choices[0].message['content']
            self.prompt_tokens += completion['usage']['prompt_tokens']
            self.completion_tokens += completion['usage']['completion_tokens']
        else:
            print("Error: Unsupported model requested")
            exit()

        self.llm_query_time += (time.time() - query_start_time)

        # Add the model's response to the messages list to keep the conversation context
        if not compile_convo or not self.used_supp_context:   # Explains it as user wrote the code
            print("**Assistant**\n" + response)
            conversation.append({
                'role': 'assistant',
                'content': response
            })

        delay = 1
        time.sleep(delay) # wait to limit API rate
        self.world_clock_time += float(delay) # ignore injected delays in performance timing
        return response

    # Generates *_spec.yaml file that acts as the formal specification document for the
    # desired design. The spec is intended to be iterated upon by the human if the resulting
    # RTL is not good enough. If being generated will also be the new acting prompt upon the
    # first shot of the compilation loop flow.
    #
    # Intended use: before the compile loop flow is initiated to bootstrap a spec doc
    def generate_spec(self, prompt: str):
        self.reset_conversations()
        (description_req, interface_req) = self.get_spec_initial_instructions(prompt)
        spec_contents  = "description: |\n  \n"
        description    = self.query_model(self.spec_conversation, description_req)
        description_lines = description.split('\n')
        indented_lines = ["  " + line for line in description_lines]
        description    = '\n'.join(indented_lines)
        spec_contents += description
        spec_contents += "\ninterface: |\n  \n  "
        spec_contents += (self.extract_codeblock(self.query_model(self.spec_conversation, interface_req))).replace('\n','\n    ')
        os.makedirs(os.path.dirname(self.spec), exist_ok=True)
        with open(self.spec, 'w') as file:
            file.write(spec_contents)
        self.dump_spec_conversation()

    # The entrypoint to the Supplemental Contexts through dictionary look-up.
    # Parses error-pattern-matching dictionary instantiated by language-specific *.yaml file
    # to give a generic example and fix of a known error message. This acts as a pseudo-RAG
    # to identify simple and consistent embeddings in compiler feedback.
    #
    # Intended use: in addition to 'compile_err' string when the Agent is using Supplemental Contexts
    def suggest_fix(self, err_msg: str):
        # Iterate over known compiler errs and check if there is a hit
        for known_error, suggested_fix in self.supplemental_contexts.items():
            if known_error in err_msg:
                return self.responses['suggest_prefix'] + suggested_fix
        # Say nothing if unrecognized err
        return ""

    # Executes the language-specific compiler script specified in the *.yaml
    # document, returning the compiler feedback and error if any were detected.
    #
    # Intended use: compile new code response from LLM that was dumped to file
    def test_compile(self):
        script  = self.compile_script + self.code
        res     = subprocess.run([script], capture_output=True, shell=True)
        errors  = self.check_errors(res)
        self.comp_n += 1
        if errors != "":
            self.comp_f += 1
        return errors

    # Executes (temp | comb)_lec script to test LLM generated RTL versus
    # golden Verilog. *_lec script uses Yosys' SAT solver methods to find
    # counter-examples to disprove equivalence. The output of the script
    # will be 'SUCCESS' if no counter-example is found, or will be a (time-step)
    # truth table dump of the golden vs gate output values.
    #
    # Intended use: for lec check after RTL has successfully compiled into Verilog
    def test_lec(self, gold: str = None, gate: str = None):
        # ./*_lec <golden_verilog> <llm_verilog>
        if gold is None:
            if self.gold is None:
                print("Error: attemping LEC without setting gold path, exiting...")
                exit()
            gold = self.gold
        if gate is None:
            gate = self.verilog
        script    = self.lec_script + " " + gold + " " + gate
        res       = subprocess.run([script], capture_output=True, shell=True)
        res_string = (str(res.stdout))[2:-1].replace("\\n","\n")
        self.lec_n += 1
        if "SUCCESS" not in res_string:
            self.lec_f += 1
            return res_string
        return ""

    # Creates Icarus Verilog testbench then executes it, carrying through the output
    # from either Icarus compiler errors or testbench results and feedback
    #
    # Intended use: for testbenching DUT after RTL has successfully compiled into Verilog
    def test_tb(self):
        script     = self.tb_script + " " + self.verilog + " " + self.tb
        res        = subprocess.run([script], capture_output=True, shell=True)
        res_string = (str(res.stdout))[2:-1].replace("\\n","\n")
        self.lec_n += 1 # is this how we want to do it?
        if "error" in res_string:   # iverilog compile error
            self.lec_f += 1
            return res_string
        return ""   #TODO: how to deal with logic mismatch??

    # Sets conversations to 'step 0', a state where no RTL generation queries have
    # been sent, only Initial Contexts if applied for compile conversation.
    #
    # Intended use: before starting a new Agent run
    def reset_conversations(self):
        self.compile_conversation = self.initial_contexts
        self.spec_conversation    = []

    # Clears all performance counters kept by Agent
    #
    # Intended use: before starting a new Agent run
    def reset_perf_counters(self):
        self.comp_n            = 0
        self.comp_f            = 0
        self.lec_n             = 0
        self.lec_f             = 0
        self.prompt_tokens     = 0
        self.completion_tokens = 0
        self.llm_query_time    = 0.0
        self.world_clock_time  = time.time()

    # The "inner loop" of the RTL generation process, attempts to iteratively
    # write compiling code in the target language through use of feedback and
    # supplemental context if enabled
    #
    # Intended use: inside of the lec loop
    def compilation_loop(self, prompt: str, iterations: int = 1):
        current_query = self.get_compile_initial_instruction(prompt)
        for _ in range(iterations):
            self.dump_code(self.query_model(self.compile_conversation, current_query, True))
            compile_out  = self.test_compile()
            print(compile_out)
            if compile_out != "":
                current_query = self.get_compile_iteration_instruction(compile_out)
            else:
                return True
        return False

    def lec_loop(self, prompt: str, lec_iterations: int = 1, compile_iterations: int = 1):
        self.reset_conversations()
        self.reset_perf_counters()
        for i in range(lec_iterations):
            if self.compilation_loop(prompt, compile_iterations):
                # Reformat is free to modify both the gold and the gate
                gold, gate = self.reformat_verilog(self.name, self.gold, self.verilog, self.io)
                lec_out = self.test_lec(gold, gate)
                if lec_out != "":
                    if i == (lec_iterations - 1):
                        self.dump_failure("lec failure\n" + lec_out)
                    else:
                        prompt = self.get_lec_fail_instruction(lec_out)
                else:
                    self.dump_compile_conversation()
                    return True
            else:
                if i == (lec_iterations - 1):
                    self.dump_failure("compile failure\n" + self.test_compile())
        self.dump_compile_conversation()
        return False

    # Helper function that reads and formats an LLM conversation
    # into a Markdown string
    #
    # Intended use: when dumping a conversation to a *_log.md file
    def format_conversation(self, conversation, start_idx: int = 0):
        md_content = ""
        for entry in conversation[start_idx:]:
            if 'role' in entry and 'content' in entry:
                role        = entry['role'].capitalize()
                content     = entry['content']
                md_content += f"**{role}:**\n{content}\n\n"
        return md_content

    def dump_spec_conversation(self):
        md_content  = "# Spec Conversation\n\n"
        md_content += self.format_conversation(self.spec_conversation)
        os.makedirs(os.path.dirname(self.spec_log), exist_ok=True)
        with open(self.spec_log, 'w') as md_file:
            md_file.write(md_content)

    def dump_compile_conversation(self):
        start_idx  = 0
        md_content = "# Compile Conversation\n\n"
        if self.used_init_context:
            md_content += "**System:** Initial contexts were appended to this conversation\n\n"
            start_idx   = len(self.initial_contexts) - 1
        md_content += self.format_conversation(self.compile_conversation, start_idx)
        if self.used_supp_context:
            # Manually 'tack on' the passing file
            with open(self.code, 'r') as f:
                last_code = f.read()
            md_content += "**Assistant:**\n" + last_code + "\n\n"
            f.close()
        md_content += self.report_statistics()
        os.makedirs(os.path.dirname(self.compile_log), exist_ok=True)
        with open(self.compile_log, 'w') as md_file:
            md_file.write(md_content)

    # Writes reason for *lec loop* failure to fail log
    #
    # Intended use: when lec_loop fails, mainly for benchmarking
    def dump_failure(self, reason: str):
        with open(self.fail_log, "w") as md_file:
            md_file.write(reason)

    def extract_codeblock(self, text:str):
        lines = text.split('\n')
        code_blocks = []
        capture = False
        block = []
        for line in lines:
            if line.strip().startswith('```'):
                if capture:
                    code_blocks.append('\n'.join(block))
                    block = []
                capture = not capture
                continue
            if capture:
                block.append(line)
        isolated_code = "\n".join(code_blocks)
        return isolated_code

    def dump_code(self, text: str):
        isolated_code = self.extract_codeblock(text)
        with open(self.code, 'w') as file:
            file.write(isolated_code)

    def report_statistics(self):
        self.world_clock_time = time.time() - self.world_clock_time
        stats = f"\nRESULTS : {self.name} : {self.comp_n} : {self.comp_f} : {self.lec_n} : {self.lec_f} : {self.prompt_tokens} : {self.completion_tokens} : {self.world_clock_time} : {self.llm_query_time}"
        return stats
