#!/usr/bin/env python3
import os
import sys
import time
import signal
from fastapi import FastAPI, HTTPException, BackgroundTasks
from starlette.requests import Request
from fastapi.responses import JSONResponse, StreamingResponse, Response, HTMLResponse
import asyncio
from threading import Thread, Event
import logging
import paramiko
from uuid import uuid4
from threading import Lock
import datetime
import select
import json

############################################################################
## This app is designed for apache with the openidc module.               ##
## It handles UUID generation and management, maintaining SSH connections ##
## and can also forward the frontend requests to the cluster's login node ##
############################################################################
## To run this app manually, execute the following command:               ##
##     uvicorn proxy:app --workers 1 --host localhost --port 8010      ##
## set user_authentication to False otherwise requests without valid UUID ##
## will be ignored                                                        ##
############################################################################


## Configuration
time_limit = 24 * 3600              # time limit for an inference ID to be valid
ROUTINE_INTERVAL = 5                # Period in seconds of sending check_routine command
ssh_timeout = 120                   # Timeout for the SSH connection
testing_mode = False                # If true, only test message is displayed
ssh_key_name = os.environ.get('KEY_NAME')
ssh_key_path = "/run/secrets/" + ssh_key_name # Path to SSH config file
allow_custom_service_names = True   # If True, the frontend can decide the service name with the "mediator-app" header
parse_headers = True                # If True, assumes curl writes headers and returns them exactly

## Log configuration
system_log = True                   # If True, log is written to syslog
file_log   = True                   # If True, log is written to file (both can be True)
log_path = '/root/log/proxy.log'     # If file_log = True, write log to this file
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
    # if system_log:
    #     s_handler = logging.handlers.SysLogHandler(address = '/dev/log')
    #     s_handler.setFormatter(syslog_format)
    #     handlers.append(s_handler)
    # ## Initialize logging
    logging.basicConfig(handlers = handlers, level=log_level)
    logging.info("Starting up...")
    initSSH()
    #task = asyncio.create_task(keep_alive())
    keep_alive_thread = KeepAliveThread()
    keep_alive_thread.start()
    logging.info("Startup complete.")
    

############################################################################
## Shutdown                                                               ##
############################################################################

def shutdown():
    """Completely shuts down the app"""
    logging.info("Shutting down...")
    os.kill(os.getpid(), signal.SIGTERM)

############################################################################
## Interacting with the HPC cluster                                       ##
############################################################################

async def run_keep_alive_command():
    try:
        stdin, stdout, stderr = ssh.exec_command("keep-alive")
        while True:
            rl, wl, xl = select.select([stdout.channel], [], [], 4*ROUTINE_INTERVAL)
            if len(rl) == 0:
                logging.error("Timeout occurred while waiting for keep-alive command to complete")
                os._exit(1)
            output = stdout.read().decode('utf-8')
            if not output:
                break
    except Exception as e:
        logging.error("Failed to keep-alive: ", e)
        initSSH()

async def keep_alive():
    """Keep the SSH connection alive."""
    logging.info("Starting task")
    while True:
        await asyncio.sleep(ROUTINE_INTERVAL)
        task = asyncio.create_task(run_keep_alive_command())
        try:
            await asyncio.wait_for(task, timeout=4*ROUTINE_INTERVAL)
        except asyncio.TimeoutError:
            logging.error("Timeout occurred while waiting for keep-alive command to complete")
            os._exit(1)

class KeepAliveThread(Thread):
    def __init__(self):
        super().__init__()
        self._stop_event = Event()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

    def run(self):
        loop = asyncio.new_event_loop()  # New event loop for this thread
        asyncio.set_event_loop(loop)
        try:
            while not self.stopped():
                loop.run_until_complete(keep_alive())  # Run keep_alive in this thread's event loop
        except asyncio.TimeoutError:
            logging.error("Timeout occurred while waiting for keep-alive command to complete")
            os._exit(1)
        except Exception as e:
            logging.error(f"Exception: {e}")
        finally:
            loop.close()

def initSSH():
    """Initialize the SSH connection."""
    try:
        global ssh
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_config = paramiko.SSHConfig()
        if not os.path.exists(ssh_key_path):
            logging.error("Couldn't find SSH key")
        # cfg = {}
        # for k in ('hostname', 'username', 'port'):
        #     if k in user_config:
        #         cfg[k] = user_config[k]

        cfg = {'hostname': os.environ.get('HPC_HOST'), 'username': os.environ.get('HPC_USER'), 'key_filename': ssh_key_path}
        # if 'user' in user_config:
        #     cfg['username'] = user_config['user']

        # # if 'proxycommand' in user_config:
        # #     cfg['sock'] = paramiko.ProxyCommand(user_config['proxycommand'])

        # if 'identityfile' in user_config:
        #     cfg['key_filename'] = user_config['identityfile']
        logging.info("SSH Configuration: " + str(cfg))
        ssh.connect(**cfg)
        # command = "keep-alive"
        # logging.info(command)
        # stdin, stdout, stderr = ssh.exec_command(command)
        # stdin.close()                       # stdin not needed               
        # stdout.channel.shutdown_write()     # indicate we're not going to write to channel
        # stdout.close()
        logging.info("SSH connection established.")
    except paramiko.AuthenticationException:
        logging.error("Authentication failed, please verify your credentials.")
    except paramiko.SSHException as sshException:
        logging.error("Unable to establish SSH connection: %s" % sshException)
    except Exception as e:
        logging.error("Exception in connecting to the server: %s" % e)

def parse_headers_curl(channel, stderr_channel):
    """Reads headers, HTTP version and status code from channel response body is reached"""
    # Parse headers
    headers={}
    http_version, status_code, reason_phrase = (None, 500, "Bad response from cloud interface")
    chunk = ''
    headers_part = b''
    reached_body = False
    while not channel.closed or channel.recv_ready() or channel.recv_stderr_ready():
        # stop if channel was closed prematurely, and there is no data in the buffers.
        if reached_body:
            break
        got_chunk = False
        readq, _, _ = select.select([channel], [], [], ssh_timeout)
        for c in readq:
            if c.recv_ready(): 
                chunk = channel.recv(len(c.in_buffer))
                headers_part += chunk
                if '\r\n\r\n' in headers_part.decode('utf-8'): # Headers finished
                    reached_body = True
                    # Split headers from body and extract HTTP information
                    status_line_and_headers, chunk = headers_part.split(b'\r\n\r\n', 1)
                    status_line_and_headers = status_line_and_headers.decode().split('\r\n')
                    status_line = status_line_and_headers.pop(0)
                    http_version, status_code, reason_phrase = status_line.split(' ', 2)
                    status_code = int(status_code)
                    # Parse headers into a dictionary
                    for pair in status_line_and_headers:
                        name, value = pair.split(': ', 1)
                        headers[name] = value
                    break
                got_chunk = True
            if c.recv_stderr_ready(): 
                # make sure to read stderr to prevent stall    
                stderr_channel.recv_stderr(len(c.in_stderr_buffer))  
                got_chunk = True  
        if not got_chunk \
            and channel.exit_status_ready() \
            and not stderr_channel.recv_stderr_ready() \
            and not channel.recv_ready(): 
            channel.shutdown_read()  # indicate we're not going to read from channel
            channel.close()  # close the channel
            break    # exit as remote side finished and buffers are empty
    return http_version, status_code, reason_phrase, headers, chunk

############################################################################
## Passthrough                                                            ##
############################################################################

@app.post("/passthrough/{path:path}", status_code=200)
@app.get("/passthrough/{path:path}", status_code=200)
async def get_hpc_response(path: str, request: Request = None) -> Response:
    """Proxy request to HPC service node"""
    method = str(request.method)
    headers = request.headers
    inference_id = headers.get('inference-id', "no-id")
    userid = headers.get('X-Consumer-Custom-ID', 'anon')
    service = headers.get('inference-service', "health")
    logging.info ("User inference request: " + str(inference_id) + " " + str(userid) + " " + service)
    try:
        data = await request.body()
    except:
        data = None
    if request.query_params:
        path += "?" + "&".join(f"{k}={v}" for k, v in request.query_params.items())
    ## Restart SSH connection if disconnected
    if not ssh.get_transport() or not ssh.get_transport().is_active():
        logging.info("SSH Connection not alive... Restarting")
        initSSH()

    headers_str = ' '.join(
        f'-H "{k}: {v}"' for k, v in headers.items() if k != 'content-length' and (k.lower() == "inference-service" or not k.lower().startswith("inference-"))  and not k.lower().startswith("x-"))
    command = (inference_id + '\n' + userid + '\n' + service + '\n' + '/' + path + f"\n -X {method} {headers_str} -d ").encode() + data
    stdin, stdout, stderr = ssh.exec_command(command)
    # stdin.write(command)
    # stdin.flush()
    # stdin.close()                       # stdin not needed               
    stdout.channel.shutdown_write()     # indicate we're not going to write to channel
    ## Build or get response headers
    headers = {}
    chunk = ''
    try:
        http_version, status_code, reason_phrase = (None, 200, "OK")
        if parse_headers:
            http_version, status_code, reason_phrase, headers, chunk = parse_headers_curl(stdout.channel, stderr.channel)
    except Exception as e:
        logging.error(str(e))
        raise HTTPException(500, "Failed to parse response from cloud interface")
    #if status_code != 200:
    #    raise HTTPException(status_code, reason_phrase)
    # Get response body
    def stream(first_chunk):
        yield first_chunk
        # chunked read to prevent stalls
        while not stdout.channel.closed or stdout.channel.recv_ready() or stdout.channel.recv_stderr_ready(): 
            # stop if channel was closed prematurely, and there is no data in the buffers.
            got_chunk = False
            readq, _, _ = select.select([stdout.channel], [], [], ssh_timeout)
            for c in readq:
                if c.recv_ready(): 
                    chunk = stdout.channel.recv(len(c.in_buffer))
                    yield chunk
                    got_chunk = True
                if c.recv_stderr_ready(): 
                    # make sure to read stderr to prevent stall    
                    stderr.channel.recv_stderr(len(c.in_stderr_buffer))  
                    got_chunk = True  
            if not got_chunk \
                and stdout.channel.exit_status_ready() \
                and not stderr.channel.recv_stderr_ready() \
                and not stdout.channel.recv_ready(): 
                stdout.channel.shutdown_read()  # indicate we're not going to read from channel
                stdout.channel.close()  # close the channel
                break    # exit as remote side finished and buffers are empty
        # close all the pseudofiles
        stdout.close()
        stderr.close()
    logging.debug(str(headers))
    return StreamingResponse(stream(chunk), headers=headers, status_code=status_code)


