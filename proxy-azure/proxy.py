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

############################################################################
## This is the OpenAI proxy. It forwards requests to external servers     ##
############################################################################
## To run this app manually, execute the following command:               ##
##     uvicorn proxy:app --workers 1 --host localhost --port <port>       ##
############################################################################

## Configuration
testing_mode = False                # If true, only test message is displayed
use_openai = True                   # If True, enables OpenAI service

## Log configuration
system_log = True                   # If True, log is written to syslog
file_log   = True                   # If True, log is written to file (both can be True)
log_path = '/root/log/proxy.log'     # If file_log = True, write log to this file
log_format = logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s - %(message)s', "%Y-%m-%d %H:%M:%S")
syslog_format = logging.Formatter('mediator: %(asctime)s.%(msecs)03d %(levelname)s - %(message)s', "%Y-%m-%d %H:%M:%S")
log_level = logging.INFO

## Reserved variables
app = FastAPI(debug=False)
openai_services = ['openai-gpt35', 'openai-gpt4']
openai_api_version = "2023-12-01-preview"  # OpenAI API version
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
    openai_key = get_secret('openai_key')
    openai_endpoint = get_secret('openai_endpoint')
    openai_deployment_name_gpt35 = get_secret('openai_deployment_name_gpt35')
    openai_deployment_name_gpt4 = get_secret('openai_deployment_name_gpt4')

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
    

############################################################################
## Shutdown                                                               ##
############################################################################

def shutdown():
    """Completely shuts down the app"""
    logging.info("Shutting down...")
    os.kill(os.getpid(), signal.SIGTERM)

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
    inference_id = headers.get('inference-id', "no-id")
    userid = headers.get('X-Consumer-Custom-ID', 'anon')
    service = headers.get('inference-service', "health")
    if service not in openai_services:
        raise HTTPException(404, "Service not found")
    logging.info ("User inference request: " + str(inference_id) + " " + str(userid) + " " + service)
    try:
        data = await request.body()
    except:
        data = None
    data = json.loads(data)
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
            if service == 'openai-gpt35':
                model = openai_deployment_name_gpt35
            elif service == 'openai-gpt4':
                model = openai_deployment_name_gpt4
            else:
                raise HTTPException(404, "Model not found")
            history = [m for m in data['messages'] if m["role"] != "system"]
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": openai_system_prompt},
                *history],
                stream=True
            )
            try:
                async for r in response:
                    if not len(r.choices) > 0 or not r.choices[0].delta or not r.choices[0].delta.content:
                        continue
                    response_str = 'data: ' + json.dumps(r.dict()) + '\n'
                    yield response_str
                    #yield r.choices[0].delta.content
            except Exception as e:
                pass # logging.error(e)
        except Exception as e:
            pass # logging.error(e)
            raise HTTPException(500, "OpenAI error")
    return StreamingResponse(stream())