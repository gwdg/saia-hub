#!/usr/bin/env python3
import os
import sys
import random
import time
import subprocess
import threading
import queue
import time
import signal
from fastapi import FastAPI, HTTPException, BackgroundTasks
from starlette.requests import Request
from fastapi.responses import JSONResponse, StreamingResponse, Response, HTMLResponse
import asyncio
from threading import Thread, Event
import logging
from uuid import uuid4
from threading import Lock
import datetime
import select
import json
import uvicorn
import uuid

############################################################################
## To run this app manually, execute the following command:               ##
##     uvicorn proxy:app --workers 1 --host localhost --port 8010         ##
############################################################################

## Configuration
ROUTINE_INTERVAL = 5                # Period in seconds of sending check_routine command
INLINE_DATA_LIMIT = 1024            # Maximum data size for which proxy will not use stdin
MAX_SSH_CONNECTIONS = 16
ssh_key_name = os.environ.get('KEY_NAME')
ssh_key_path = "/run/secrets/" + ssh_key_name # Path to SSH config file
parse_headers = True                # If True, assumes curl writes headers and returns them exactly
use_stdio = False                   # If True, sends all inputs through stdin. Required for large inputs e.g. files.
enable_accounting = True            # If True, injects include_usage and counts tokens
extract_model = True                # If True, extracts model name from JSON body

## Log configuration
file_log   = True                   # If True, log is written to file
current_month = datetime.datetime.now().strftime("%Y-%m") # Get the current month and year
log_path = f"/root/log/proxy-{current_month}.log"         # If file_log = True, write log to this file
log_format = logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s - %(message)s', "%Y-%m-%d %H:%M:%S")
syslog_format = logging.Formatter('mediator: %(asctime)s.%(msecs)03d %(levelname)s - %(message)s', "%Y-%m-%d %H:%M:%S")
log_level = logging.INFO

## Reserved variables
app = FastAPI(debug=False)

############################################################################
## Startup                                                                ##
############################################################################

def get_secret(secret_name):
    try:
        with open(f'/run/secrets/{secret_name}', 'r') as secret_file:
            return secret_file.read().rstrip('\n')
    except IOError:
        return None

@app.on_event("startup")
async def startup_event():
    """Initialize the model when the server starts."""
    handlers = []
    if file_log:
        f_handler = logging.FileHandler(log_path)
        f_handler.setFormatter(log_format)
        handlers.append(f_handler)
    ## Initialize logging
    logging.basicConfig(handlers = handlers, level=log_level)
    logging.info("Starting up...")
    keep_alive_thread = KeepAliveThread()
    keep_alive_thread.start()
    logging.info("Startup complete.")


############################################################################
## Shutdown                                                               ##
############################################################################

def shutdown():
    """Completely shuts down the app"""
    logging.info("Shutting down...")
    os.kill(os.getppid(), signal.SIGTERM)

############################################################################
## Interacting with the HPC cluster                                       ##
############################################################################

keep_alive_event = asyncio.Event()
keep_alive_event.set()

async def keep_alive():
    while True:
        try:
            proc = await run_ssh_command("keep-alive")
            await proc.wait()
            await asyncio.sleep(ROUTINE_INTERVAL)
        except Exception as e:
            logging.error(f"Keep-alive failed: {str(e)}")
            await asyncio.sleep(5)

class KeepAliveThread(Thread):
    def __init__(self):
        super().__init__()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

    def run(self):
        loop = asyncio.new_event_loop()  # New event loop for this thread
        asyncio.set_event_loop(loop)
        try:
            # while keep_alive_event.is_set():
            while not self._stop_event.is_set():
                logging.info("Starting keep-alive loop")
                loop.run_until_complete(keep_alive())  # Run keep_alive in this thread's event loop
        except asyncio.TimeoutError:
            logging.error("Timeout 3 occurred while waiting for keep-alive command to complete")
        except Exception as e:
            logging.error(f"Exception: {e}")
        finally:
            loop.close()

def parse_headers_curl(channel: bytes) -> tuple:
    """Reads headers, HTTP version and status code from channel response body is reached"""
    # Initialize variables
    headers = {}
    http_version, status_code, reason_phrase = (None, 500, "Bad response from cloud interface")
    headers_part = channel  # Treat channel as the entire response bytes
    reached_body = False
    chunk = b''

    # Check if headers are present
    if b'\r\n\r\n' in headers_part:
        # Split headers and body
        status_line_and_headers, chunk = headers_part.split(b'\r\n\r\n', 1)
        status_line_and_headers = status_line_and_headers.decode().split('\r\n')
        status_line = status_line_and_headers.pop(0)
        
        # Parse status line
        try:
            http_version, status_code, reason_phrase = status_line.split(' ', 2)
            status_code = int(status_code)
            if status_code == 100:  # 100 Continue
                # Reset headers and continue reading
                headers = {}
                return parse_headers_curl(chunk)

        except ValueError:
            # Handle malformed status line
            pass

        # Parse headers
        for pair in status_line_and_headers:
            if not pair:
                continue
            try:
                name, value = pair.split(': ', 1)
                if name.lower() != 'content-length':
                    headers[name] = value
            except ValueError:
                # Handle malformed header
                pass

        reached_body = True

    logging.debug("Finished parse headers successfully")
    return http_version, status_code, reason_phrase, headers, chunk


############################################################################
## Accounting                                                             ##
############################################################################

import json

def extract_tokens(response):
    input_tokens = 0
    output_tokens = 0
    try:
        response = response.decode()
    except Exception as e:
        logging.error("Failed to decode response")
        return input_tokens, output_tokens

    # Split the response into individual SSE events
    events = response.split('\n\n')
    found_usage = False
    # Iterate over the events in reverse order to find the last valid JSON occurrence
    for event in reversed(events):
        try:
            # Check if the event starts with "data: "
            if event.startswith('data: '):
                # Remove the "data: " prefix and parse the JSON payload
                payload = json.loads(event[6:])  # skip the 'data: ' prefix
            else:
                # Parse the JSON payload directly
                payload = json.loads(event)

            if 'usage' in payload:
                found_usage = True
                usage = payload['usage']
                input_tokens = usage.get('prompt_tokens', 0)
                output_tokens = usage.get('completion_tokens', 0)
                total_tokens = usage.get('total_tokens', 0)
                break
        except json.JSONDecodeError:
            pass
    if not found_usage:
        logging.error("No usage data found.")
    return input_tokens, output_tokens


############################################################################
## Passthrough                                                            ##
############################################################################

async def run_ssh_command(remote_command, data=None):
    # SSH command to execute
    ssh_cmd = [
        'ssh',
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'LogLevel=ERROR',
        '-o', 'ControlMaster=auto',
        '-o', f'ControlPath=/tmp/ssh-{random.randint(0, MAX_SSH_CONNECTIONS)}-%r@%h:%p',
        '-o', 'ControlPersist=4h',
        '-i', '/run/secrets/kisski-ssh-key',
        os.environ.get("HPC_USER") + '@' + os.environ.get("HPC_HOST"),
        remote_command
    ]
    
    
    proc = await asyncio.create_subprocess_exec(
        *ssh_cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    if data:
        proc.stdin.write(data)
        await proc.stdin.drain()
        proc.stdin.close()

    return proc

@app.options("/passthrough/{path:path}", status_code=200)
@app.post("/passthrough/{path:path}", status_code=200)
@app.get("/passthrough/{path:path}", status_code=200)
async def get_hpc_response(path: str, request: Request = None) -> Response:
    """Proxy request to HPC service node"""
    global enable_accounting, extract_model
    proceed_accounting = enable_accounting
    method = str(request.method)
    headers = request.headers
    try:
        data = await request.body()
    except:
        data = None
    if request.query_params:
        path += "?" + "&".join(f"{k}={v}" for k, v in request.query_params.items())

    ## Try to extract name of service
    service = headers.get('inference-service', None)

    ## Softly force include usage
    if enable_accounting or extract_model:
        try:
            data_json = json.loads(data)
            ## Inject include usage if streaming
            if enable_accounting and "stream" in data_json and data_json["stream"]:
                data_json["stream_options"] = {"include_usage": True}
                data = json.dumps(data_json).encode()
            if extract_model and not service:
                if "model" in data_json:
                    service = data_json["model"]
        except json.JSONDecodeError as e:
            logging.warning("Failed to parse JSON data - Accounting not available")
            proceed_accounting = False
            #raise HTTPException(status_code=400, detail="Invalid JSON data")
        except Exception as e:
            logging.warning("Failed to understand data - Accounting not available.")
            proceed_accounting = False
            #raise HTTPException(status_code=400, detail="Bad request")

    if not service:
        raise HTTPException(status_code=400, detail="Service or model not specified")
    
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
        'service': service,
        'input_size': len(data) if data else 0,
        'start_timestamp': datetime.datetime.now().isoformat(),
        'portal': headers.get('inference-portal', 'SAIA'),
        'status': "PENDING",
    }
    logging.info("Inference Request: " + json.dumps(inference))

    # Extract important headers
    headers_str = ' '.join(
        f'-H "{k}: {v}"' for k, v in headers.items() if k.lower() != 'content-length' and (k.lower() == "inference-service" or not k.lower().startswith("inference-"))  and not k.lower().startswith("x-"))
    
    # Build the remote command
    command = (inference['id'] + '\n' + inference['uid'] + '\n' + inference['service'] + '\n' + '/' + path + f"\n -X {method} {headers_str}")
    
    # Determine if data should be sent inline
    is_parsable = False
    try:
        decoded_data = data.decode('utf-8')
    except:
        is_parsable = False
    
    data_remains = False
    if data and is_parsable and len(data) <= INLINE_DATA_LIMIT and not use_stdio:
        remote_command = (command + ' -d ').encode() + data
    else:
        remote_command = command.encode()
        data_remains = True
    
    # Start the async subprocess
    proc = await run_ssh_command(remote_command, data)

    # Read headers first
    header_buffer = b''
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            header_buffer += chunk
            if b'\r\n\r\n' in header_buffer:
                break
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "Timeout waiting for headers")

    # Parse headers from the buffer
    try:
        http_version, status_code, reason_phrase, headers, body_chunk = parse_headers_curl(header_buffer)
    except Exception as e:
        proc.kill()
        raise HTTPException(502, f"Bad gateway: {str(e)}")
    
    async def stream_generator():
        try:
            # Yield the initial body chunk from header parsing
            full_response = b''
            if body_chunk:
                yield body_chunk
                full_response += body_chunk
            
            # Stream remaining data
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
                full_response += chunk
            # Collect stderr for logging
            # stderr = await proc.stderr.read()
            # if stderr:
            #    logging.debug(f"SSH stderr: {stderr.decode()}")
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        inference['end_timestamp'] = datetime.datetime.now().isoformat()
        inference['status'] = 'COMPLETED'
        inference['output_size'] = len(full_response)
        try:
            input_tokens, output_tokens = extract_tokens(full_response) if proceed_accounting else (0,0)
            inference['input_tokens'] = input_tokens
            inference['output_tokens'] = output_tokens
        except Exception as e:
            logging.warning("Failed to extract tokens.")
        logging.info("Inference Response: " + json.dumps(inference))
        await proc.wait()
    
    return StreamingResponse(
        stream_generator(),
        headers=headers,
        status_code=status_code
    )

if __name__ == '__main__':
    uvicorn.run(
        "proxy:app",
        workers=int(os.environ.get("WORKERS", 1)),
        loop="uvloop",
        timeout_keep_alive=120,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),  # Default port to 8000 if $PORT is not set
        log_config="./log_conf.yaml",
        log_level="info",  # Adjust as needed
        reload=False,      # Optional, for development
    )