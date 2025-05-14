#!/usr/bin/env python3
import os
import sys
import time
import signal
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from fastapi.responses import JSONResponse, StreamingResponse, Response, HTMLResponse
import asyncio
import logging
import paramiko
from uuid import uuid4
from threading import Lock
import datetime
import select
from openai import AsyncAzureOpenAI as AzureOpenAI
import json
import threading
import uuid
import tiktoken
import uvicorn
import base64
from PIL import Image
import io
import re


############################################################################
## This is the OpenAI proxy. It forwards requests to external servers     ##
############################################################################
## To run this app manually, execute the following command:               ##
##     uvicorn proxy:app --workers 1 --host localhost --port <port>       ##
############################################################################

## Configuration
testing_mode = False                # If true, only test message is displayed
use_openai = True                   # If True, enables OpenAI service
enable_accounting = True            # If True, counts tokens

## Log configuration
system_log = True                   # If True, log is written to syslog
file_log   = True                   # If True, log is written to file (both can be True)
# Get the current month and year
current_month = datetime.datetime.now().strftime("%Y-%m")
log_path = f"/root/log/proxy-{current_month}.log"     # If file_log = True, write log to this file
log_format = logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s - %(message)s', "%Y-%m-%d %H:%M:%S")
syslog_format = logging.Formatter('mediator: %(asctime)s.%(msecs)03d %(levelname)s - %(message)s', "%Y-%m-%d %H:%M:%S")
log_level = logging.INFO

## Reserved variables
app = FastAPI(debug=False)
openai_services = ['openai-gpt41', 'openai-gpt41-mini', 'openai-gpt4o-mini', 'openai-gpt4o', 'openai-o1', 'openai-o3', 'openai-o1-mini', 'openai-o3-mini', 'openai-o4-mini']
openai_api_version = "2024-12-01-preview"  # OpenAI API version
openai_system_prompt =  """You are an intelligent chatbot hosted by GWDG to help users answer their scientific questions.
    Instructions: 
    - Only respond to scientific or serious requests where you can actually provide assistance. Avoid sensitive topics.
    - If you're unsure of an answer, you can say "I don't know" or "I'm not sure" and recommend the user to ask an expert for more information.
    - If a user asks for medical advice, always recommend to seek professional medical advice and not to rely on information from the internet.
"""

############################################################################
## Startup                                                                ##
############################################################################

def get_secret(secret_name):
    try:
        with open(f'/run/secrets/{secret_name}', 'r') as secret_file:
            return secret_file.read().rstrip('\n')
    except IOError:
        return None

if use_openai:
    openai_config = json.loads(get_secret('openai_config'))
    openai_key = openai_config["openai_key"]
    openai_endpoint = openai_config["openai_endpoint"]
    openai_deployment_name_gpt41_mini = openai_config['openai_deployment_name_gpt41_mini']
    openai_deployment_name_gpt41 = openai_config['openai_deployment_name_gpt41']
    openai_deployment_name_gpt4o_mini = openai_config['openai_deployment_name_gpt4o_mini']
    openai_deployment_name_gpt4o = openai_config['openai_deployment_name_gpt4o']
    openai_deployment_name_o1_mini = openai_config['openai_deployment_name_o1_mini']
    openai_deployment_name_o1 = openai_config['openai_deployment_name_o1']
    openai_deployment_name_o3 = openai_config['openai_deployment_name_o3']
    openai_deployment_name_o3_mini = openai_config['openai_deployment_name_o3_mini']
    openai_deployment_name_o4_mini = openai_config['openai_deployment_name_o4_mini']

@app.on_event("startup")
async def startup_event():
    """Initialize the model when the server starts."""
    global openai_client, use_openai
    ## Create log handlers
    handlers = []
    if file_log:
        f_handler = logging.FileHandler(log_path)
        f_handler.setFormatter(log_format)
        handlers.append(f_handler)
    # if system_log:
    #     s_handler = logging.handlers.SysLogHandler(address = '/dev/log')
    #     s_handler.setFormatter(syslog_format)
    #     handlers.append(s_handler)
    # ## Initialize logging
    logging.basicConfig(handlers = handlers, level=log_level)
    logging.info("Startup complete.")
    logging.debug("Secrets:")
    logging.debug(openai_key)
    logging.debug(openai_deployment_name_gpt41_mini)
    logging.debug(openai_deployment_name_gpt41)
    logging.debug(openai_deployment_name_gpt4o)
    logging.debug(openai_deployment_name_o1)
    logging.debug(openai_deployment_name_o1_mini)
    

############################################################################
## Shutdown                                                               ##
############################################################################

def shutdown():
    """Completely shuts down the app"""
    logging.info("Shutting down...")
    os.kill(os.getpid(), signal.SIGTERM)

############################################################################
## Accounting                                                             ##
############################################################################

def calculate_image_token(width, height):#https://platform.openai.com/docs/guides/vision#:~:text=Calculating%20costs&text=The%20token%20cost%20of%20a,square%2C%20maintaining%20their%20aspect%20ratio
    #scaled to fit within a 2048 x 2048 square,maintaining their aspect ratio.
    
    if max(width, height) > 2048:
        scale = min(2048 / width, 2048 / height)
        scaled_width = int(width * scale)
        scaled_height = int(height * scale)
    else: 
        scaled_height, scaled_width = height, width 

    scale = 768 / min(scaled_width, scaled_height)    
    final_width = int(scaled_width * scale)
    final_height = int(scaled_height * scale)
    tiles = (final_width + 511) // 512 * (final_height + 511) // 512
    return tiles * 170 + 85

def extract_tokens(messages, model="gpt-3.5-turbo-0613"):
    """
    Return the number of tokens used by a list of messages.
    """
    
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        print("Warning: model not found. Using cl100k_base encoding.")
        encoding = tiktoken.get_encoding("cl100k_base")

    if model in {
        "gpt-3.5-turbo-0613",
        "gpt-3.5-turbo-16k-0613",
        "gpt-4-0314",
        "gpt-4-32k-0314",
        "gpt-4-0613",
        "gpt-4-32k-0613",
        "gpt-4o",
        "gpt-4o-mini",
        "o1",
        "chat-academic-cloud-gpt35",
        "chat-academic-cloud-gpt4",
        "chat-academic-cloud-gpt41",
        "chat-academic-cloud-gpt41-mini",
        "chat-academic-cloud-gpt4o",
        "chat-academic-cloud-gpt4o-mini",
        "chat-academic-cloud-o1-mini",
        "chat-academic-cloud-o1-preview",
        "chat-academic-cloud-o1",
        "chat-academic-cloud-o3-mini",
        "chat-academic-cloud-o4-mini",
        }:
        tokens_per_message = 3
        tokens_per_name = 1
    elif model == "gpt-3.5-turbo-0301":
        tokens_per_message = 4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        tokens_per_name = -1  # if there's a name, the role is omitted
    elif "gpt-3.5-turbo" in model:
        return extract_tokens(messages, model="gpt-3.5-turbo-0613")
    elif "gpt-4" in model or "gpt4" in model:
        return extract_tokens(messages, model="gpt-4-0613")
    elif "gpt-4o" in model:
        return extract_tokens(messages, model="gpt-4o")
    elif "openai-o1" in model:
        return extract_tokens(messages, model="o1")
    else:
        raise NotImplementedError(
            f"""num_tokens_from_messages() is not implemented for model {model}. See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens."""
        )
    
    num_tokens = 0

    if type(messages) == list:
        for message in messages:
            if type(message['content'])==list: #list sizs is always 2: [{'type': 'text', 'text': 'hi\n'}, {'type': 'image_url', 'image_url': {'url': 'da
                if message['content'][1]['type']=='image_url':
                    logging.debug("----------image detected and base64 is: ")
                    logging.debug(message['content'][1]['image_url']['url'])
                    image_seg=message['content'][1]['image_url']['url']
                    match = re.search(r"data:image/(.*);base64,(.*)", image_seg)
                    image_type = match.group(1)
                    base64_image = match.group(2)
                    logging.debug(base64_image)
                    decoded_image = base64.b64decode(base64_image)
                    image = io.BytesIO(decoded_image)
                    image = Image.open(image)
                    width, height = image.size
                    logging.debug(f"Image dimensions: {width}x{height}")
                    num_tokens=calculate_image_token(width, height)
                    return num_tokens
            else:
                num_tokens += tokens_per_message
                for key, value in message.items():
                    num_tokens += len(encoding.encode(value))
                    if key == "name":
                        num_tokens += tokens_per_name
                num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    elif type(messages) == str:
        num_tokens += len(encoding.encode(messages))
    return num_tokens

############################################################################
## Passthrough                                                            ##
############################################################################

@app.post("/passthrough/{path:path}", status_code=200)
async def get_openai_response(path: str, request: Request = None) -> StreamingResponse:
    """Send message and history to and get response from OpenAI"""
    if not use_openai:
        raise HTTPException(403, "Service locked by administrator")
    method = str(request.method)
    if method == 'GET':
        return Response("OK", 200)
    headers = request.headers
    try:
        data = await request.body()
    except:
        data = None
    data = json.loads(data)

    user_o = None
    user_ou = None

    if 'x-consumer-groups' in headers:
        groups = headers['x-consumer-groups'].split(',')
        for group in groups:
            group = group.strip()
            if group.startswith('org_'):
                user_o = group[4:]
            elif group.startswith('orgunit_'):
                user_ou = group[8:]

    inference = {
        'id': headers.get('inference-id', str(uuid.uuid4())),
        'uid': headers.get('X-Consumer-Custom-ID', 'anon'),
        'o': user_o,
        'ou': user_ou,
        'service': headers.get('inference-service', None),
        'input_size': len(data) if data else 0,
        'start_timestamp': datetime.datetime.now().isoformat(),
        'portal': "Chat AI",
        'status': "PENDING",
    }
    logging.info("Inference Request: " + json.dumps(inference))

    if inference['service'] not in openai_services:
        raise HTTPException(404, "Service not found")
    async def stream():
        try:
            logging.debug("Activating OpenAI client")
            client = AzureOpenAI(
                api_key=openai_key,
                api_version=openai_api_version,
                azure_endpoint=openai_endpoint,
            )
        except Exception as e:
            logging.error("Could not activate OpenAI client: " + str(e))
            return
        try:
            logging.debug(f"inference service is: {inference['service']}")
            if inference['service'] == 'openai-gpt41':
                model = openai_deployment_name_gpt41
            elif inference['service'] == 'openai-gpt41-mini':
                model = openai_deployment_name_gpt41_mini
            elif inference['service'] == 'openai-gpt4o-mini':
                model = openai_deployment_name_gpt4o_mini
            elif inference['service'] == 'openai-gpt4o':
                model = openai_deployment_name_gpt4o
            elif inference['service'] == 'openai-o1-mini':
                model = openai_deployment_name_o1_mini
            elif inference['service'] == 'openai-o1':
                model = openai_deployment_name_o1
            elif inference['service'] == 'openai-o3':
                model = openai_deployment_name_o3
            elif inference['service'] == 'openai-o3-mini':
                model = openai_deployment_name_o3_mini
            elif inference['service'] == 'openai-o4-mini':
                model = openai_deployment_name_o4_mini
            else:
                raise HTTPException(404, "Model not found")
            full_response = ''
            history = [m for m in data['messages'] if m["role"] != "system"]
            if "o1" not in model:
                messages=[{"role": "system", "content": openai_system_prompt},*history]
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True
                )
                try:
                    async for r in response:
                        if not len(r.choices) > 0 or not r.choices[0].delta or not r.choices[0].delta.content:
                            continue
                        full_response += r.choices[0].delta.content
                        response_str = 'data: ' + json.dumps(r.dict()) + '\n'
                        yield response_str
                        #yield r.choices[0].delta.content
                    inference['status'] = 'COMPLETED'
                except Exception as e:
                    inference['status'] = 'FAILED'
                    pass # logging.error(e)
            else:
                messages=[{"role": "user", "content": openai_system_prompt + "\n" + history[0]["content"]}, *(history[1:])]
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=False
                )
                try:
                    message = response.choices[0].message.dict()
                    full_response = message["content"]
                    choice = response.choices[0].dict()
                    response = response.dict()
                    completion_tokens = response["usage"]["completion_tokens"]
                    prompt_tokens = response["usage"]["prompt_tokens"]
                    total_tokens = response["usage"]["total_tokens"]
                    for r_char in full_response:
                        r = {"id": response["id"],
                            "choices": [{"delta": {"content": str(r_char),
                                                    "function_call": message["function_call"],
                                                    "role": None,
                                                    "tool_calls": message["tool_calls"]},
                                            "finish_reason": None,
                                            "index": choice["index"],
                                            "logprobs": choice["logprobs"],
                                            "content_filter_results":choice["content_filter_results"]}],
                            "created": response["created"],
                            "model": response["model"],
                            "object": "chat.completion.chunk",
                            "system_fingerprint": response["system_fingerprint"]
                        }
                        response_str = 'data: ' + json.dumps(r) + '\n'
                        yield response_str
                        #yield r.choices[0].delta.content
                    inference['status'] = 'COMPLETED'
                except Exception as e:
                    inference['status'] = 'FAILED'
                    #logging.error(e)
        except Exception as e:
            #logging.error(e)
            inference['status'] = 'FAILED'
            raise HTTPException(500, "OpenAI error")
        finally:
            inference['end_timestamp'] = datetime.datetime.now().isoformat()
            inference['output_size'] = len(full_response)
            if "o1" not in model:
                input_tokens, output_tokens = 0,0
                try:
                    input_tokens = extract_tokens(messages, model) if enable_accounting else 0
                    output_tokens = extract_tokens(full_response, model) if enable_accounting else 0
                except Exception as e:
                    logging.warning("Failed to extract tokens: " + str(e))
                inference['input_tokens'] = input_tokens
                inference['output_tokens'] = output_tokens
            else:
                inference['input_tokens'] = prompt_tokens
                inference['output_tokens'] = completion_tokens
            logging.info("Inference Response: " + json.dumps(inference))
    return StreamingResponse(stream())

if __name__ == '__main__':
    uvicorn.run(
        "proxy:app",
        workers=int(os.environ.get("WORKERS", 1)),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),  # Default port to 8000 if $PORT is not set
        log_config="./log_conf.yaml",
        log_level="info",  # Optional, adjust as needed
        reload=False,  # Optional, for development
    )
