import http
import json
import base64
import requests
import pyotp
from urllib.parse import parse_qs, urlparse
from fyers_apiv3 import fyersModel
import time
import hashlib
import os

'''
====================================================
send_login_otp_v2
       ↓
verify_otp
       ↓
verify_pin_v2
       ↓
   intermediate access_token(Authorization: Bearer <token-from-verify_pin_v2>)
       ↓
POST /api/v3/token
       ↓
     auth_code
       ↓
POST /api/v3/validate-authcode
       ↓
 FINAL access_token
       ↓
GET /api/v3/profile
GET /api/v3/funds
====================================================
 
'''

# ==========================================
# ENTER YOUR FYERS CREDENTIALS HERE
# ==========================================
APP_ID = "YFLRWVHU2G"       # Example: 'EGNI8CE27Q' YFLRWVHU2G-100
APP_SECRET = "HJ5RUXDPW2"   #HJ5RUXDPW2

#===================================================
APP_TYPE = "100"             # Usually '100'
FY_ID = "YY01479"      # Example: 'XY12345'
TOTP_KEY = "U6DLWH4CW64MFZJV4KOOI6F47WG55U4W" # 32-character base32 secret from Fyers account settings
PIN = "1436"      # e.g., '1234'
REDIRECT_URI = "http://127.0.0.1:5000/fyers/ai_callback/" # Example: 'https://trade.fyers.in/api-login/redirect-uri/index.html'
TOKEN_FILE = "fyers_token.txt"

## ==========================================
#          BASE URLS FOR FYERS API V3
## ==========================================
BASE_URL = 'https://api-t2.fyers.in/vagator/v2'
BASE_URL_2 = 'https://api-t1.fyers.in/api/v3'
URL_VERIFY_CLIENT_ID = BASE_URL + '/send_login_otp_v2'  # send_login_otp
URL_VERIFY_TOTP = BASE_URL + '/verify_otp'
URL_VERIFY_PIN = BASE_URL + '/verify_pin_v2'  # verify_pin
URL_VERIFY_TOKEN = "https://api-t1.fyers.in/gk/verify_token"  # verify_token
URL_VALIDATE_TOKENS = "https://api-t1.fyers.in/gk/validate_tokens"  # validate_tokens
URL_TOKEN = BASE_URL_2 + '/token'  # "https://api-t1.fyers.in/api/v3/token"
URL_VALIDATE_AUTH_CODE = BASE_URL_2 + '/validate-authcode'
SUCCESS = 1
ERROR = -1


def encode_base64(input_string):
    """Encodes a string to base64 required by Vagator v2 endpoints."""
    return base64.b64encode(str(input_string).encode('ascii')).decode('ascii')


def sha256_hash(appId, appType, appSecret):

    message = f"{appId}-{appType}:{appSecret}"
    message = message.encode()
    sha256_hash = hashlib.sha256(message).hexdigest()

    return sha256_hash


def validate_authcode(session, app_id, app_secret, auth_code):
    try:
        app_id_hash = sha256_hash(appId=app_id, appType=APP_TYPE, appSecret=app_secret)

        payload = {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code,
        }

        headers = {
                    "Content-Type": "application/json",
                }
        
        print(f"Validate Auth Code Headers: {headers}")
  
        result_string = session.post(
            url=URL_VALIDATE_AUTH_CODE,
            json=payload,
            headers=headers,
        )
        print(f"Validate Auth Code Response: {result_string.status_code} - {result_string.text}")
        if result_string.status_code != 200:
            return [ERROR, result_string.text]

        result = json.loads(result_string.text)
        access_token = result["access_token"]
        return [SUCCESS, access_token]

    except Exception as e:
        return [ERROR, e]
   
    
def verify_client_id(session, fy_id: str, app_id="2"):
    """Sends the base64 encoded user ID and App ID to initiate the login process."""
    try:
        payload = {
            "fy_id": encode_base64(fy_id),
            "app_id": app_id
        }
        result_string = session.post(url=URL_VERIFY_CLIENT_ID, json=payload)
        print(f"Verify Client ID Response: {result_string.status_code} - {result_string.text}")
        if result_string.status_code != 200:
            return [ERROR, result_string.text]
            
        result = json.loads(result_string.text)
        request_key = result["request_key"]
        return [SUCCESS, request_key]
    except Exception as e:
        return [ERROR, str(e)]
    
    
def verify_totp(session, request_key, totp):
    """Verifies the generated TOTP against the request key."""
    try:
        payload = {
            "request_key": request_key,
            "otp": totp
        }
        result_string = session.post(url=URL_VERIFY_TOTP, json=payload)
        print(f"Verify TOTP Response: {result_string.status_code} - {result_string.text}")
        if result_string.status_code != 200:
            return [ERROR, result_string.text]
            
        result = json.loads(result_string.text)
        return [SUCCESS, result["request_key"]]
    except Exception as e:
        return [ERROR, str(e)]
 
 
def verify_pin(session, request_key, pin):
    """Verifies the user's base64 encoded PIN and returns the temporary trade access token."""
    try:
        payload = {
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": encode_base64(pin)
        }
        result_string = session.post(url=URL_VERIFY_PIN, json=payload)
        print(f"Verify PIN Response: {result_string.status_code} - {result_string.text}")
        if result_string.status_code != 200:
            return [ERROR, result_string.text]
            
        result = json.loads(result_string.text)
        access_token = result["data"]["access_token"]
        return [SUCCESS, access_token]
    except Exception as e:
        return [ERROR, str(e)]   
 
 
def get_auth_code(session, fy_id, app_id, redirect_uri, app_type, access_token):
    """Retrieves the auth code using the temporary trade access token."""
    try:
        payload = {
            "fyers_id": fy_id,
            "app_id": app_id, # Pass clean APP_ID without the '-100' here
            "redirect_uri": redirect_uri,
            "appType": app_type,
            "code_challenge": "",
            "state": "None",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        result_string = session.post(url=URL_TOKEN, json=payload, headers=headers)
        print(f"Get Auth Code Response: {result_string.status_code} - {result_string.text}")
        
        if result_string.status_code != 308: # Expecting a redirect response
            return [ERROR, result_string.text]
            
        result = json.loads(result_string.text)
        url = result["Url"]
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        auth_code = query_params.get("auth_code", [None])[0]
        if not auth_code:
            return [ERROR, "Auth code not found in the response URL."]
        return [SUCCESS, auth_code]
    except Exception as e:
        return [ERROR, str(e)]
    
    
def load_cached_token(file_path=TOKEN_FILE):
    """Reads existing access token from local file if it exists."""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                token = f.read().strip()
                return token if token else None
        except Exception as e:
            print(f"Error reading token file: {e}")
            return None
    return None


def save_token_to_file(token, file_path=TOKEN_FILE):
    """Saves the fresh access token to local file."""
    try:
        with open(file_path, "w") as f:
            f.write(token)
        print(f"Token saved successfully to '{file_path}'.")
    except Exception as e:
        print(f"Failed to save token to file: {e}")


def is_token_valid(app_id):
    """
    Validates token validity against Fyers API by checking profile.
    Returns (True, profile_data) if valid, (False, None) otherwise.
    """
    token = load_cached_token(file_path=f"{app_id}_{TOKEN_FILE}")
    if not token:
        return False, None
    
    print("Found existing token in file. Verifying validity...")
    
    try:
        fyers = fyersModel.FyersModel(
            client_id=f"{app_id}-{APP_TYPE}",
            is_async=False,
            token=token,
            log_path=""
        )
        profile = fyers.get_profile()
        print(f"Profile Response: {json.dumps(profile, indent=2)}")
        # Fyers API returns s="ok" on valid token, and s="error" on expired/invalid token
        if profile.get("s") == "ok":
            return True, fyers
        return False, None
    except Exception as e:
        print(f"Token validation check encountered an error: {e}")
        return False, None
       
        
def get_fyers_final_session_handle(fy_id,
                                   totp_key,
                                   pin,
                                   redirect_uri,
                                   app_id,
                                   app_secret):
    """Main workflow to generate the final Fyers API V3 Access Token."""
    print("Starting Fyers Auto-Login Process...")
    
    # CRITICAL: We must use a Session object to persist cookies across all Vagator API calls.
    # Without this, Step 4 will fail with the 'must use the fyers APIv3 APIs' error.
    session = requests.Session()
    
    try:
        """
        Checks if an existing token in fyers_token.txt is still valid.
        Only triggers auto-login and generates a fresh token if the existing one is missing or expired.
        """
        
        valid, fyres = is_token_valid(app_id)
        if valid:
            print(" Existing token is valid! Skipping full login.")
            return fyres
        print("⚠️ Cached token has expired or is invalid.")
        #return cached_token
        
        print("Proceeding to request a new access token...")
        print("Requesting new access token...")
        # Step 1: Send Login OTP Request
        print("Step 1: Sending Login Request...")
        send_otp_res = verify_client_id(session, fy_id)
        if send_otp_res[0] != SUCCESS:
            print(f"Failed to send login request: {send_otp_res[1]}")
            return None
        request_key = send_otp_res[1]
        
        # Step 2: Verify TOTP
        print("Step 2: Verifying TOTP...")
        totp = pyotp.TOTP(totp_key).now()
        print(f"Generated TOTP: {totp}")
        verify_totp_res = verify_totp(session, request_key, totp)
        if verify_totp_res[0] != SUCCESS:
            print(f"Failed to verify TOTP: {verify_totp_res[1]}")
            return None
        request_key_2 = verify_totp_res[1]
                
        # Step 3: Verify PIN
        print("Step 3: Verifying PIN...")
        verify_pin_res = verify_pin(session, request_key_2, pin)
        if verify_pin_res[0] != SUCCESS:
            print(f"Failed to verify PIN: {verify_pin_res[1]}")
            return None
        trade_access_token = verify_pin_res[1]
          
        # Step 4: Add Bearer token to Session headers and Get Auth Code
        print("Step 4: Retrieving Auth Code...")
        session.headers.update({
            'Authorization': f"Bearer {trade_access_token}",
            'Content-Type': 'application/json'
        })
        print(f"Session Headers Updated with Bearer Token: {session.headers}")
        
        auth_code_res = get_auth_code(session, fy_id, app_id, redirect_uri, APP_TYPE, trade_access_token)
        if auth_code_res[0] != SUCCESS:
            print(f"Failed to retrieve auth_code: {auth_code_res[1]}")
            return None
        auth_code = auth_code_res[1]
                
        # Step 5: Exchange Auth Code for Final API V3 Access Token
        print("Step 5: Generating Final Access Token...")       
        validate_authcode_res = validate_authcode(session, app_id, app_secret, auth_code)
        print(f"Validate Auth Code Response: {validate_authcode_res}")
        if validate_authcode_res[0] != SUCCESS:
            print(f"Failed to validate auth_code: {validate_authcode_res[1]}")
            return None
        
        final_token = validate_authcode_res[1]  # This is the final access token
  
        if (ERROR != validate_authcode_res[0]):
            print("\n✅ Authentication Successful!")
            print("-" * 50)
            print(f"ACCESS TOKEN: {final_token}")
            print("-" * 50)
            
            # Optional: Save to file for reuse in your trading bots
            save_token_to_file(final_token, file_path=f"{app_id}_{TOKEN_FILE}")
            print(f"Token saved to '{app_id}_{TOKEN_FILE}' for future use.")
                
            return fyersModel.FyersModel(client_id=f"{app_id}-{APP_TYPE}", token=final_token, is_async=False, log_path="")
        else:
            print(f"❌ Failed to generate access token: {validate_authcode_res}")
            return None

    except Exception as e:
        print(f"An exception occurred during authentication: {e}")
        return None


def get_fyers_session_handle():
    """Wrapper function to get Fyers session handle using global constants."""
    return get_fyers_final_session_handle(
        fy_id=FY_ID,
        totp_key=TOTP_KEY,
        pin=PIN,
        redirect_uri=REDIRECT_URI,
        app_id=APP_ID,
        app_secret=APP_SECRET
    )
 
    
if __name__ == "__main__":
    
    fyers = get_fyers_final_session_handle(
        fy_id=FY_ID,
        totp_key=TOTP_KEY,
        pin=PIN,
        redirect_uri=REDIRECT_URI,
        app_id=APP_ID,
        app_secret=APP_SECRET,
    )
    
    if fyers:
        print("\nTesting Token with Fyers Profile API...")
        profile = fyers.get_profile()
        print(json.dumps(profile, indent=2))