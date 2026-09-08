
import json


data = "{'code': '', 'data': {'appName': 'APP_for_AI_AotoM', 'app_id': 'YFLRWVHU2G', 'auth': 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJbXCJkOjFcIixcImQ6MlwiLFwieDowXCIsXCJ4OjFcIl0iLCJhdF9oYXNoIjoiZ0FBQUFBQnFub0dwZnpTX3hvb2xRVy13SjQ0cmVKdE44UHMtcG5FOGd4QWpRdjJ0TmVXelZVYlRSUTVpRzE0ei1PRHpFdWpYaGVWR2VNY2JydGM2NFVsOWlZcEw0UU9QbS1zX21YTERXR244TU01MmxkYk81TjA9IiwiZGlzcGxheV9uYW1lIjoiWVkwMTQ3OSIsIm9tcyI6IksxIiwiaHNtX2tleSI6ImQ0NDE0NDcyMDE1MGQ1OTdiNDc0NzA5MTk4ZGI1OTA3MmJjYWExOTg0MjVmY2YxMDhkOGQ0OThkIiwiaXNEZHBpRW5hYmxlZCI6Ik4iLCJpc010ZkVuYWJsZWQiOiJOIiwiZXhwIjoxNzg4NzczMDc3LCJpYXQiOjE3ODg3NzI3NzcsImlzcyI6ImFwaS5meWVycy5pbiIsIm5iZiI6MTc4ODc3Mjc3Nywic3ViIjoiYWNjZXNzX3Rva2VuIn0.oUGkXFH6gbdnlo_TxKeebN2ha3xSP8_h4qWxR-bEiUw', 'image': '', 'nonce': '', 'permissions': ['d:1', 'd:2', 'x:0', 'x:1'], 'redirectUrl': 'http://127.0.0.1:5000/fyers/ai_callback/', 'response_type': 'code', 'scope': '', 'state': 'None', 'user_id': 'YY01479'}, 'message': '', 's': 'ok'}"
data_json = json.loads(data.replace("'", '"'))
print(data_json)
print(f"Auth Token: {data_json['data']['auth']}")
print(f"Redirect URL: {data_json['data']['redirectUrl']}")
result_auth = data_json['data']['auth']
auth_code = result_auth.rsplit(".", 1)[0]
print(f"Auth Code: {auth_code}")

'''
import requests
import hashlib

app_id = "YOUR_APP_ID"
secret_id = "YOUR_SECRET_ID"
auth_code = "xxxx.yyyyyyyy.zzzzzzzz"

app_id_hash = hashlib.sha256(
    f"{app_id}:{secret_id}".encode()
).hexdigest()

url = "https://api-t1.fyers.in/api/v3/validate-authcode"

payload = {
    "grant_type": "authorization_code",
    "appIdHash": app_id_hash,
    "code": auth_code
}

headers = {
    "Content-Type": "application/json"
}

response = requests.post(
    url,
    json=payload,
    headers=headers
)

print(response.status_code)
print(response.text)

import requests
import hashlib

app_id = "YOUR_APP_ID"
secret_id = "YOUR_SECRET_ID"
auth_code = "xxxx.yyyyyyyy.zzzzzzzz"

app_id_hash = hashlib.sha256(
    f"{app_id}:{secret_id}".encode()
).hexdigest()

url = "https://api-t1.fyers.in/api/v3/validate-authcode"

payload = {
    "grant_type": "authorization_code",
    "appIdHash": app_id_hash,
    "code": auth_code
}

headers = {
    "Content-Type": "application/json"
}

response = requests.post(
    url,
    json=payload,
    headers=headers
)

print(response.status_code)
print(response.text)

'''
