"""
Entrypoint for Gradio, see https://gradio.app/
"""

import platform
import asyncio
import base64
import os
import json
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import cast, Dict, AsyncGenerator, Generator

import gradio as gr
from anthropic import APIResponse
from anthropic.types import TextBlock
from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaToolUseBlock
from anthropic.types.tool_use_block import ToolUseBlock
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import JSONResponse
from threading import Thread
import uvicorn

from screeninfo import get_monitors
from fastapi.middleware.cors import CORSMiddleware
import nest_asyncio
from fastapi.responses import StreamingResponse
from openai import OpenAI

nest_asyncio.apply()

# TODO: I don't know why If don't get monitors here, the screen resolution will be wrong for secondary screen. Seems there are some conflict with computer_use_demo.tools
screens = get_monitors()
print(screens)
from computer_use_demo.loop import (
    PROVIDER_TO_DEFAULT_MODEL_NAME,
    APIProvider,
    # sampling_loop,
    sampling_loop_sync,
)

from computer_use_demo.tools import ToolResult
from computer_use_demo.tools.computer import get_screen_details

CONFIG_DIR = Path("~/.anthropic").expanduser()
API_KEY_FILE = CONFIG_DIR / "api_key"

WARNING_TEXT = "⚠️ Security Alert: Never provide access to sensitive accounts or data, as malicious web content can hijack Claude's behavior"

SELECTED_SCREEN_INDEX = None
SCREEN_NAMES = None
ANTHROPIC_KEY = "*************************FILL THE API KEY************************"


class Sender(StrEnum):
    USER = "user"
    BOT = "assistant"
    TOOL = "tool"


def setup_state(state):
    if "messages" not in state:
        state["messages"] = []
    if "api_key" not in state:
        # Try to load API key from file first, then environment
        state[
            "api_key"] = ANTHROPIC_KEY
        if not state["api_key"]:
            print("API key not found. Please set it in the environment or storage.")
    if "provider" not in state:
        state["provider"] = os.getenv("API_PROVIDER", "anthropic") or APIProvider.ANTHROPIC
    if "provider_radio" not in state:
        state["provider_radio"] = state["provider"]
    if "model" not in state:
        _reset_model(state)
    if "auth_validated" not in state:
        state["auth_validated"] = False
    if "responses" not in state:
        state["responses"] = {}
    if "tools" not in state:
        state["tools"] = {}
    if "only_n_most_recent_images" not in state:
        state["only_n_most_recent_images"] = 2  # 10
    if "custom_system_prompt" not in state:
        state["custom_system_prompt"] = load_from_storage("system_prompt") or ""
        # remove if want to use default system prompt
        device_os_name = "Windows" if platform.system() == "Windows" else "Mac" if platform.system() == "Darwin" else "Linux"
        state["custom_system_prompt"] += f"\n\nNOTE: you are operating a {device_os_name} machine"
    if "hide_images" not in state:
        state["hide_images"] = False


def _reset_model(state):
    state["model"] = PROVIDER_TO_DEFAULT_MODEL_NAME[cast(APIProvider, state["provider"])]


async def main(state):
    """Render loop for Gradio"""
    setup_state(state)
    return "Setup completed"


def validate_auth(provider: APIProvider, api_key: str | None):
    if provider == APIProvider.ANTHROPIC:
        if not api_key:
            return "Enter your Anthropic API key to continue."
    if provider == APIProvider.BEDROCK:
        import boto3

        if not boto3.Session().get_credentials():
            return "You must have AWS credentials set up to use the Bedrock API."
    if provider == APIProvider.VERTEX:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        if not os.environ.get("CLOUD_ML_REGION"):
            return "Set the CLOUD_ML_REGION environment variable to use the Vertex API."
        try:
            google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except DefaultCredentialsError:
            return "Your google cloud credentials are not set up correctly."


def load_from_storage(filename: str) -> str | None:
    """Load data from a file in the storage directory."""
    try:
        file_path = CONFIG_DIR / filename
        if file_path.exists():
            data = file_path.read_text().strip()
            if data:
                return data
    except Exception as e:
        print(f"Debug: Error loading {filename}: {e}")
    return None


def save_to_storage(filename: str, data: str) -> None:
    """Save data to a file in the storage directory."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = CONFIG_DIR / filename
        file_path.write_text(data)
        # Ensure only user can read/write the file
        file_path.chmod(0o600)
    except Exception as e:
        print(f"Debug: Error saving {filename}: {e}")


def _api_response_callback(response: APIResponse[BetaMessage], response_state: dict):
    response_id = datetime.now().isoformat()
    response_state[response_id] = response


def _tool_output_callback(tool_output: ToolResult, tool_id: str, tool_state: dict):
    tool_state[tool_id] = tool_output


def _render_message(sender: Sender, message: str | BetaTextBlock | BetaToolUseBlock | ToolResult, state):
    is_tool_result = not isinstance(message, str) and (
            isinstance(message, ToolResult)
            or message.__class__.__name__ == "ToolResult"
            or message.__class__.__name__ == "CLIResult"
    )
    if not message or (
            is_tool_result
            and state["hide_images"]
            and not hasattr(message, "error")
            and not hasattr(message, "output")
    ):
        return
    if is_tool_result:
        message = cast(ToolResult, message)
        if message.output:
            return message.output
        if message.error:
            return f"Error: {message.error}"
        if message.base64_image and not state["hide_images"]:
            return base64.b64decode(message.base64_image)
    elif isinstance(message, BetaTextBlock) or isinstance(message, TextBlock):
        return message.text
    elif isinstance(message, BetaToolUseBlock) or isinstance(message, ToolUseBlock):
        return f"Tool Use: {message.name}\nInput: {message.input}"
    else:
        return message


# open new tab, open google sheets inside, then create a new blank spreadsheet

def process_input(user_input, state):
    if state is None:
        state = {}
    # Ensure the state is properly initialized
    setup_state(state)

    # Append the user input to the messages in the state
    state["messages"].append(
        {
            "role": Sender.USER,
            "content": [TextBlock(type="text", text=user_input)],
        }
    )

    yield yield_message(state), state

    # for message in yield_message(state):
    #     yield json.dumps(message), state

    # for message in yield_message(state):
    #     yield message, state

    # results = []
    # for message in yield_message(state):
    #     # Convert message or other non-serializable parts to JSON-friendly structures
    #     results.append(message)
    #
    # return results, state


def accumulate_messages(*args, **kwargs):
    """
    Wrapper function to accumulate messages from sampling_loop_sync.
    """
    accumulated_messages = []
    global SELECTED_SCREEN_INDEX
    print(f"Selected screen: {SELECTED_SCREEN_INDEX}")
    messages = sampling_loop_sync(*args, selected_screen=SELECTED_SCREEN_INDEX, **kwargs)
    for message in messages:
        # Check if the message is already in the accumulated messages
        if message not in accumulated_messages:
            accumulated_messages.append(message)
            # Yield the accumulated messages as a list to check the uniqueness of the mess
            yield message


def yield_message(state):
    # Ensure the API key is present
    if not state.get("api_key"):
        raise ValueError("API key is missing. Please set it in the environment or storage.")

    # Call the sampling loop and yield messages
    for message in accumulate_messages(
            system_prompt_suffix=state["custom_system_prompt"],
            model=state["model"],
            provider=state["provider"],
            messages=state["messages"],
            output_callback=partial(_render_message, Sender.BOT, state=state),
            tool_output_callback=partial(_tool_output_callback, tool_state=state["tools"]),
            api_response_callback=partial(_api_response_callback, response_state=state["responses"]),
            api_key=state["api_key"],
            only_n_most_recent_images=state["only_n_most_recent_images"],
    ):
        yield message


with gr.Blocks(theme=gr.themes.Soft()) as demo:
    state = gr.State({})  # Use Gradio's state management

    # Retrieve screen details
    gr.Markdown("Wirtual AI")

    # if not os.getenv("HIDE_WARNING", False):
    #     gr.Markdown(WARNING_TEXT)

    with gr.Accordion("Settings", open=False):
        with gr.Row():
            with gr.Column():
                model = gr.Textbox(label="Model", value="claude-3-5-sonnet-20241022")
            with gr.Column():
                provider = gr.Dropdown(
                    label="API Provider",
                    choices=[option.value for option in APIProvider],
                    value="anthropic",
                    interactive=True,
                )
            with gr.Column():
                api_key = gr.Textbox(
                    label="Anthropic API Key",
                    type="password",
                    value="",
                    interactive=True,
                )
            with gr.Column():
                custom_prompt = gr.Textbox(
                    label="System Prompt Suffix",
                    value="",
                    interactive=True,
                )
            with gr.Column():
                screen_options, primary_index = get_screen_details()
                SCREEN_NAMES = screen_options
                SELECTED_SCREEN_INDEX = primary_index
                screen_selector = gr.Dropdown(
                    label="Select Screen",
                    choices=screen_options,
                    value=screen_options[primary_index] if screen_options else None,
                    interactive=True,
                )
            with gr.Column():
                only_n_images = gr.Slider(
                    label="N most recent screenshots",
                    minimum=0,
                    value=2,
                    interactive=True,
                )
        # hide_images = gr.Checkbox(label="Hide screenshots", value=False)

    # Define the merged dictionary with task mappings
    merged_dict = json.load(open("examples/ootb_examples.json", "r"))


    # Callback to update the second dropdown based on the first selection
    def update_second_menu(selected_category):
        return gr.update(choices=list(merged_dict.get(selected_category, {}).keys()))


    # Callback to update the third dropdown based on the second selection
    def update_third_menu(selected_category, selected_option):
        return gr.update(choices=list(merged_dict.get(selected_category, {}).get(selected_option, {}).keys()))


    # Callback to update the textbox based on the third selection
    def update_textbox(selected_category, selected_option, selected_task):
        task_data = merged_dict.get(selected_category, {}).get(selected_option, {}).get(selected_task, {})
        prompt = task_data.get("prompt", "")
        preview_image = task_data.get("initial_state", "")
        task_hint = "Task Hint: " + task_data.get("hint", "")
        return prompt, preview_image, task_hint


    # Function to update the global variable when the dropdown changes
    def update_selected_screen(selected_screen_name):
        global SCREEN_NAMES
        global SELECTED_SCREEN_INDEX
        SELECTED_SCREEN_INDEX = SCREEN_NAMES.index(selected_screen_name)
        print(f"Selected screen updated to: {SELECTED_SCREEN_INDEX}")


    with gr.Accordion("Quick Start Prompt", open=False):  # open=False 表示默认收
        # Initialize Gradio interface with the dropdowns
        with gr.Row():
            # Set initial values
            initial_category = "Game Play"
            initial_second_options = list(merged_dict[initial_category].keys())
            initial_third_options = list(merged_dict[initial_category][initial_second_options[0]].keys())
            initial_text_value = merged_dict[initial_category][initial_second_options[0]][initial_third_options[0]]

            with gr.Column(scale=2):
                # First dropdown for Task Category
                first_menu = gr.Dropdown(
                    choices=list(merged_dict.keys()), label="Task Category", interactive=True, value=initial_category
                )

                # Second dropdown for Software
                second_menu = gr.Dropdown(
                    choices=initial_second_options, label="Software", interactive=True, value=initial_second_options[0]
                )

                # Third dropdown for Task
                third_menu = gr.Dropdown(
                    # choices=initial_third_options, label="Task", interactive=True, value=initial_third_options[0]
                    choices=["Please select a task"] + initial_third_options, label="Task", interactive=True,
                    value="Please select a task"
                )

            with gr.Column(scale=1):
                image_preview = gr.Image(label="Reference Initial State", height=260 - (318.75 - 280))
                hintbox = gr.Markdown("Task Hint: Selected options will appear here.")

        # Textbox for displaying the mapped value
        # textbox = gr.Textbox(value=initial_text_value, label="Action")

    api_key.change(fn=lambda key: save_to_storage(API_KEY_FILE, key), inputs=api_key)

    with gr.Row():
        # submit_button = gr.Button("Submit")  # Add submit button
        with gr.Column(scale=8):
            chat_input = gr.Textbox(show_label=False, placeholder="Type a message to send to Computer Use OOTB...",
                                    container=False)
        with gr.Column(scale=1, min_width=50):
            submit_button = gr.Button(value="Send", variant="primary")

    chatbot = gr.Chatbot(label="Chatbot History", autoscroll=True, height=580)

    screen_selector.change(fn=update_selected_screen, inputs=screen_selector, outputs=None)

    # Link callbacks to update dropdowns based on selections
    first_menu.change(fn=update_second_menu, inputs=first_menu, outputs=second_menu)
    second_menu.change(fn=update_third_menu, inputs=[first_menu, second_menu], outputs=third_menu)
    third_menu.change(fn=update_textbox, inputs=[first_menu, second_menu, third_menu],
                      outputs=[chat_input, image_preview, hintbox])

    # chat_input.submit(process_input, [chat_input, state], chatbot)
    submit_button.click(process_input, [chat_input, state], chatbot)

app = FastAPI()

origins = [
    "http://localhost:3000",  # React development server (replace with your React URL if different)
    "http://localhost:3005",  # React development server (replace with your React URL if different)
    "https://your-react-app.com",  # Production React app URL (optional)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # Allows all origins, or specify as needed
    allow_credentials=True,
    allow_methods=["*"],  # Allows all HTTP methods (GET, POST, PUT, DELETE, etc.)
    allow_headers=["*"],  # Allows all headers
)


@app.post("/api/process-input", response_class=StreamingResponse)
async def process_input_api(request: Request):
    data = await request.json()
    user_input = data.get("user_input")
    api_key = request.headers.get("anthropic-api-key")

    if not api_key:
        raise HTTPException(status_code=401, detail="API Key missing")

    print("Processing input...", user_input)

    # Create the state and process inputanthr=
    state = {}
    setup_state(state)

    # Ensure the state is properly initialized
    state["api_key"] = api_key

    # Append the user input to the messages in the state
    state["messages"].append(
        {
            "role": Sender.USER,
            "content": [TextBlock(type="text", text=user_input)],
        }
    )

    # Call the sampling loop and yield messages
    def message_generator() -> Generator:
        try:
            # Use accumulate_messages to yield individual messages
            for message in accumulate_messages(
                    system_prompt_suffix=state["custom_system_prompt"],
                    model=state["model"],
                    provider=state["provider"],
                    messages=state["messages"],
                    output_callback=partial(_render_message, Sender.BOT, state=state),
                    tool_output_callback=partial(_tool_output_callback, tool_state=state["tools"]),
                    api_response_callback=partial(_api_response_callback, response_state=state["responses"]),
                    api_key=state["api_key"],
                    only_n_most_recent_images=state["only_n_most_recent_images"],
            ):
                # Ensure each message is in the proper SSE format
                yield f"data: {message}\n\n"
        except Exception as e:
            yield f"data: Error: {str(e)}\n\n"

    # Return the streaming response in the correct content type
    return StreamingResponse(message_generator(), media_type="text/event-stream")

# @app.post("/api/process-input-using-model", response_class=StreamingResponse)
# async def process_input_api_using_model(request: Request):
openai_client = OpenAI(api_key="***************************************************************")
@app.post("/api/process_input_api_using_model", response_class=StreamingResponse)
async def process_input_loop(user_input: str = Form(...)):
    async def response_generator():
        try:
            # Initialize the conversation log
            conversation_log = []

            # Step 1: Initialize the state and get the initial breakdown from OpenAI
            openai_response = openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "Break down the user's input into actionable steps."},
                    {"role": "user", "content": user_input},
                ],
                max_tokens=500,
            )

            current_prompt = openai_response.choices[0].message.content.strip()
            conversation_log.append({"role": "OpenAI", "content": current_prompt})
            yield f"data: OpenAI Processed Input:\n{current_prompt}\n\n"

            state = {}
            setup_state(state)
            state["api_key"] = "**********************************************************************"

            while True:
                # Step 2: Send the current prompt to Anthropic and stream the response
                state["messages"].append(
                    {
                        "role": Sender.USER,
                        "content": [TextBlock(type="text", text=current_prompt)],
                    }
                )

                try:
                    for message in accumulate_messages(
                            system_prompt_suffix=state["custom_system_prompt"],
                            model=state["model"],
                            provider=state["provider"],
                            messages=state["messages"],
                            output_callback=partial(_render_message, Sender.BOT, state=state),
                            tool_output_callback=partial(_tool_output_callback, tool_state=state["tools"]),
                            api_response_callback=partial(_api_response_callback, response_state=state["responses"]),
                            api_key=state["api_key"],
                            only_n_most_recent_images=state["only_n_most_recent_images"],
                    ):
                        conversation_log.append({"role": "Anthropic", "content": message})
                        yield f"data: Anthropic Response: {message}\n\n"

                except Exception as e:
                    error_message = f"Error from Anthropic: {str(e)}"
                    conversation_log.append({"role": "Anthropic", "content": error_message})
                    yield f"data: {error_message}\n\n"
                    break

                # Step 3: Ask OpenAI if further action is needed
                openai_followup = openai_client.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role": "system", "content": "Based on the response, determine if more action is needed. If yes, provide the next prompt. If no, state 'FINAL RESPONSE'."},
                        {"role": "user", "content": f"Anthropic response:\n{message}"},
                    ],
                    max_tokens=200,
                )

                followup_content = openai_followup.choices[0].message.content.strip()
                conversation_log.append({"role": "OpenAI", "content": followup_content})
                yield f"data: OpenAI Follow-Up:\n{followup_content}\n\n"

                # Print the conversation log to the console
                print(json.dumps({
                    "conversation": conversation_log
                }, indent=2))

                # Check if OpenAI says it's the final response
                if "FINAL RESPONSE" in followup_content.upper():
                    yield f"data: Conversation closed by OpenAI.\n\n"
                    break

                # Update the prompt for the next loop iteration
                current_prompt = followup_content

        except Exception as e:
            error_message = f"Error: {str(e)}"
            conversation_log.append({"role": "System", "content": error_message})
            print(json.dumps({
                "timestamp": str(datetime.datetime.now()),
                "conversation": conversation_log
            }, indent=2))
            yield f"data: {error_message}\n\n"

    return StreamingResponse(response_generator(), media_type="text/event-stream")

# def process_input(user_input, state):
#     if state is None:
#         state = {}
#     # Initialize state if not already done
#     if "messages" not in state:
#         state["messages"] = []
#     # Process the input (this is just a placeholder example)
#     response = f"Processed: {user_input}"
#     state["messages"].append({"user": user_input, "response": response})
#
#     # Return the response and the updated state
#     return response, state


def run_gradio():
    # Define Gradio interface with state as both input and output
    demo = gr.Interface(
        fn=process_input,
        inputs=[gr.Textbox(label="Enter Input"), gr.State()],
        outputs=[gr.Textbox(), gr.State()]
    )
    demo.launch(share=True, server_name="localhost", server_port=7861)


def run_fastapi():
    uvicorn.run("app:app", host="localhost", port=8001)


if __name__ == "__main__":
    # Start FastAPI in a separate thread
    fastapi_thread = Thread(target=run_fastapi)
    fastapi_thread.start()

    # Run Gradio in the main thread
    run_gradio()
