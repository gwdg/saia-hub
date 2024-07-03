import requests
import json
import subprocess
import os
import sys

# Get the email address from the user
email = input("Please enter the user's email address:\n")

# Search for the user in the Kong consumer DB
response = requests.get(f"http://localhost:8001/consumers/{email}")

# Parse the JSON response
data = json.loads(response.content)

# Check if the user was found
if "id" not in data.keys():
    print("ERROR: User not found")
    exit()

print("Found user #" + data["id"])

full_name = input("Please enter the user's full name:\n")

# Get the ticket number from the user
ticket_number = input("Please enter the user's ticket number:\n")

# Add the "api-user" ACL group to the user
consumer_id = data["id"]
response = requests.post(f"http://localhost:8001/consumers/{consumer_id}/acls", json={"group": "api-user"})

if response.status_code == 409:
        print("WARNING: User already had API access!")
elif response.status_code not in [200, 201]:  
    print("Error adding ACL to user")
    exit()

# Generate a random MD5 key
key = subprocess.check_output("head --bytes 100 /dev/random | md5sum", shell=True).decode("utf-8").strip().split()[0]
print("Generated key: ", key)

# Get the TTL as input, default to 6 months (15552000 seconds)
ttl = int(input("Enter the TTL (in seconds) for the API key, or press enter for default (6 months): ") or 15552000)

# Add the key to the user as a key-auth key with TTL
response = requests.post(f"http://localhost:8001/consumers/{consumer_id}/key-auth", json={"key": key, "ttl": ttl, "tags": [ticket_number]})

if response.status_code not in [200, 201]:
    print("Error adding key to user")
    exit()

# Test the key by sending a curl request
headers = {
    "Accept": "application/json",
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json"
}

data = {
    "model": "intel-neural-chat-7b",
    "prompt": "San Fransico is a",
    "max_tokens": 7,
    "temperature": 0
}

response = requests.post("https://chat-ai.academiccloud.de/v1/completions", headers=headers, json=data)

if response.status_code not in [200]:
    print("Error testing key: ", response.status_code)
    exit()
else:
     print("Key created and tested successfully: ", response.json()['choices'][0]['text'])

# Ask the user for their preferred language for the template email
lang = input("Would you like the template email in English (en) or German (de)? ")

# Fill in the correct template accordingly
if lang == "en":
    template = u"""------
    To: {email}
    Subject: [SAIA] Your Chat AI API Key
------
Dear {full_name},
 
We're pleased to inform you that your Chat AI API key has been created, granting you access to our Scalable AI Accelerator (SAIA) platform. Please ensure the security of your API key and only share it with team members involved in your project.  Here are the access details to the API service:
 
API key: {key}
API endpoint: https://chat-ai.academiccloud.de/v1
Available models:
- meta-llama-3-8b-instruct
- mixtral-8x7b-instruct
- meta-llama-3-70b-instruct
- qwen2-72b-instruct
 
Note that these details may change in the future. To receive updates on SAIA's latest developments, we strongly recommend subscribing to our mailing list:
https://listserv.gwdg.de/mailman/listinfo/ai-saia-users
 
Our service is OpenAI-compatible. Therefore, similar to OpenAI, we provide the following APIs:
- v1/completions for text generation and completion
- v1/chat/completions for user-assistant conversations
- v1/models for the list of available models
 
To test your API access, you can run this command on a UNIX machine:
 
Text completion API:
```
curl -i -N -X POST \
  --url https://chat-ai.academiccloud.de/v1/completions \
  --header 'Accept: application/json' \
  --header 'Authorization: Bearer <key>' \
  --header 'Content-Type: application/json'\
  --data '{                     
  "model": "meta-llama-3-8b-instruct",
  "prompt": "San Fransico is a",
  "max_tokens": 7,
  "temperature": 0
}'
```
 
Chat completion API:
```
curl -i -N -X POST \
  --url https://chat-ai.academiccloud.de/v1/chat/completions \
  --header 'Accept: application/json' \
  --header 'Authorization: Bearer <key>' \
  --header 'Content-Type: application/json'\
  --data '{                     
  "model": "meta-llama-3-8b-instruct",
  "messages": [{"role":"system","content":"You are a helpful assistant"},{"role":"user","content":"How tall is the Eiffel tower?"}],
  "temperature": 0
}'
```
 
Remember to replace <key> with your actual API key. Once you have verified that the key works, you can use it in any OpenAI-compatible software or tool, by simply setting the API key and endpoint. Here is an example of a python client using the `openai` package:
 
```
from openai import OpenAI
  
# API configuration
api_key = '<api_key>' # Replace with your API key
base_url = "https://chat-ai.academiccloud.de/v1"
model = "meta-llama-3-8b-instruct" # Choose any available model
  
# Start OpenAI client
client = OpenAI(
    api_key = api_key,
    base_url = base_url
)
  
# Get response
chat_completion = client.chat.completions.create(
        messages=[{"role":"system","content":"You are a helpful assistant"},{"role":"user","content":"How tall is the Eiffel tower?"}],
        model= model,
    )
  
# Print full response as JSON
print(chat_completion) # You can extract the response text from the JSON object
```
 
If you have any more questions, feel free to ask.
"""
elif lang == "de":
    template = u"""------
    To: {email}
    Subject: [SAIA] Your Chat AI API Key
------
Sehr geehrte*r {full_name},
 
Wir freuen uns, Ihnen mitteilen zu können, dass Ihr Chat-AI-API-Schlüssel erstellt wurde, was Ihnen Zugriff auf unsere skalierbare AI-Accelerator-Plattform (SAIA) ermöglicht. Bitte stellen Sie sicher, dass Ihr API-Schlüssel sicher ist, und teilen Sie ihn nur mit Teammitgliedern, die an Ihrem Projekt beteiligt sind.
 
Hier sind die Zugangsdetails für den API-Dienst:
 
API key: {key}
API-Endpunkt: https://chat-ai.academiccloud.de/v1
Verfügbare Modelle:
- meta-llama-3-8b-instruct
- mixtral-8x7b-instruct
- meta-llama-3-70b-instruct
- qwen2-72b-instruct
 
Bitte beachten Sie, dass sich die Modellliste in der Zukunft ändern kann. Um über die neuesten Entwicklungen von SAIA auf dem Laufenden zu bleiben, empfehlen wir Ihnen dringend, unseren Mailing-Liste zu abonnieren:
https://listserv.gwdg.de/mailman/listinfo/ai-saia-users
 
Unser Dienst ist OpenAI-kompatibel. Daher bieten wir, ähnlich wie OpenAI, zwei Haupt-APIs an:
- v1/completions für Textgenerierung und -vervollständigung
- v1/chat/completions für Benutzer-Assistenten-Gespräche
- v1/models für die Liste der verfügbaren Modelle
 
Um Ihren API-Zugang zu testen, können Sie diesen Befehl auf einer UNIX-Maschine ausführen:
 
text-completion API:
```
curl -i -N -X POST \
  --url https://chat-ai.academiccloud.de/v1/completions \
  --header 'Accept: application/json' \
  --header 'Authorization: Bearer <key>' \
  --header 'Content-Type: application/json'\
  --data '{                     
  "model": "meta-llama-3-8b-instruct",
  "prompt": "San Fransico is a",
  "max_tokens": 7,
  "temperature": 0
}'
```
 
chat-completion API:
```
curl -i -N -X POST \
  --url https://chat-ai.academiccloud.de/v1/chat/completions \
  --header 'Accept: application/json' \
  --header 'Authorization: Bearer <key>' \
  --header 'Content-Type: application/json'\
  --data '{                     
  "model": "meta-llama-3-8b-instruct",
  "messages": [{"role":"system","content":"You are a helpful assistant"},{"role":"user","content":"How tall is the Eiffel tower?"}],
  "temperature": 0
}'
```
 
Bitte beachten Sie, <key> durch Ihren tatsächlichen API-Schlüssel zu ersetzen. Sobald Sie überprüft haben, dass der Schlüssel funktioniert, können Sie ihn in jeder OpenAI-kompatiblen Software oder jedem Tool verwenden, indem Sie einfach den API-Schlüssel und den Endpunkt einstellen. Hier ist ein Beispiel für einen Python-Client, der das openai-Paket verwendet:
 
```
from openai import OpenAI
  
# API configuration
api_key = '<api_key>' # Replace with your API key
base_url = "https://chat-ai.academiccloud.de/v1"
model = "meta-llama-3-8b-instruct" # Choose any available model
  
# Start OpenAI client
client = OpenAI(
    api_key = api_key,
    base_url = base_url
)
  
# Get response
chat_completion = client.chat.completions.create(
        messages=[{"role":"system","content":"You are a helpful assistant"},{"role":"user","content":"How tall is the Eiffel tower?"}],
        model= model,
    )
  
# Print full response as JSON
print(chat_completion) # You can extract the response text from the JSON object
```
 
Wenn Sie weitere Fragen haben, können Sie diese gerne stellen.
"""
else:
    print("Invalid language selection")
    exit()

# Echo the filled-in template
try:
    sys.stdout.buffer.write(template.replace("{email}", email).replace("{key}", key).replace("{full_name}",full_name).encode('utf-8'))
    sys.stdout.buffer.flush()
except:
    print("Error while generating email from template, but was created!")
