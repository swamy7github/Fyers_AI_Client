import json
import requests
import pyotp
from urllib.parse import parse_qs, urlparse
from fyers_apiv3 import fyersModel
import sys
import time

# ==========================================
# ENTER YOUR FYERS CREDENTIALS HERE
# ==========================================
APP_ID = "YFLRWVHU2G"       # Example: 'EGNI8CE27Q'
APP_TYPE = "100"             # Usually '100'
SECRET_KEY = "HJ5RUXDPW2" 
FY_ID = "YY01479"      # Example: 'XY12345'
TOTP_KEY = "U6DLWH4CW64MFZJV4KOOI6F47WG55U4W" # 32-character base32 secret from Fyers account settings
PIN = "1436"      # e.g., '1234'
REDIRECT_URI = "http://127.0.0.1:5000/fyers/ai_callback" # Example: 'https://trade.fyers.in/api-login/redirect-uri/index.html'

# ==========================================
# FYERS API V3 LOGIN ENDPOINTS
# ==========================================
BASE_URL_VAGATOR = "https://api-t2.fyers.in/vagator/v2"
BASE_URL_API = "https://api-t1.fyers.in/api/v3"

URL_SEND_LOGIN_OTP = BASE_URL_VAGATOR + "/send_login_otp_v2"
URL_VERIFY_TOTP = BASE_URL_VAGATOR + "/verify_otp"
URL_VERIFY_PIN = BASE_URL_VAGATOR + "/verify_pin_v2"
URL_TOKEN = BASE_URL_API + "/token"

# Status constants
SUCCESS = 1
ERROR = -1
import base64

def encode_base64(input_string):
    """Encodes a string to base64 required by Vagator v2 endpoints."""
    return base64.b64encode(input_string.encode('ascii')).decode('ascii')

def generate_totp(secret):
    """Generates the current 6-digit TOTP code using the secret key."""
    try:
        generated_totp = pyotp.TOTP(secret).now()
        return [SUCCESS, generated_totp]
    except Exception as e:
        return [ERROR, str(e)]

def send_login_otp(fy_id, app_id):
    """Initiates the login process by sending the base64 encoded user ID and App ID."""
    try:
        payload = {
            "fy_id": encode_base64(fy_id),
            "app_id": app_id
        }
        result_string = requests.post(url=URL_SEND_LOGIN_OTP, json=payload)
        
        if result_string.status_code != 200:
            return [ERROR, result_string.text]
            
        result = json.loads(result_string.text)
        request_key = result["request_key"]
        return [SUCCESS, request_key]
    except Exception as e:
        return [ERROR, str(e)]

def verify_totp(request_key, totp):
    """Verifies the generated TOTP against the request key."""
    try:
        payload = {
            "request_key": request_key,
            "otp": totp
        }
        result_string = requests.post(url=URL_VERIFY_TOTP, json=payload)
        
        if result_string.status_code != 200:
            return [ERROR, result_string.text]
            
        result = json.loads(result_string.text)
        return [SUCCESS, result["request_key"]]
    except Exception as e:
        return [ERROR, str(e)]

def verify_pin(request_key, pin):
    """Verifies the user's base64 encoded PIN and returns the temporary trade access token."""
    try:
        payload = {
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": encode_base64(pin)
        }
        result_string = requests.post(url=URL_VERIFY_PIN, json=payload)
        
        if result_string.status_code != 200:
            return [ERROR, result_string.text]
            
        result = json.loads(result_string.text)
        access_token = result["data"]["access_token"]
        return [SUCCESS, access_token]
    except Exception as e:
        return [ERROR, str(e)]

def get_auth_code(fy_id, app_id, redirect_uri, app_type, access_token):
    """Exchanges the temporary access token for an OAuth authorization code."""
    try:
        payload = {
            "fyers_id": fy_id,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "appType": app_type,
            "code_challenge": "",
            "state": "sample_state",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True
        }
        headers = {'Authorization': f'Bearer {access_token}'}
        result_string = requests.post(url=URL_TOKEN, json=payload, headers=headers)
        
        if result_string.status_code != 200:
            return [ERROR, result_string.text]
            
        result = json.loads(result_string.text)
        url = result["Url"]
        
        # Extract the auth_code from the returned redirect URL
        parsed = urlparse(url)
        auth_code = parse_qs(parsed.query)['auth_code'][0]
        
        return [SUCCESS, auth_code]
    except Exception as e:
        return [ERROR, str(e)]

def get_final_access_token():
    """Main workflow to generate the final Fyers API V3 Access Token."""
    
    print("Step 1: Generating TOTP...")
    totp_res = generate_totp(TOTP_KEY)
    if totp_res[0] != SUCCESS:
        print(f"Failed to generate TOTP: {totp_res[1]}")
        sys.exit()
    totp = totp_res[1]
    
    # Send Login OTP Request
    print("Step 2: Sending Login Request...")
    # Fyers expects AppId in format "APPID-APPTYPE" for this endpoint
    app_id_type = "2" # 2 denotes web login for Vagator APIs
    send_otp_res = send_login_otp(FY_ID, f"{APP_ID}-{app_id_type}")
    
    if send_otp_res[0] != SUCCESS:
        print(f"Failed to send login request: {send_otp_res[1]}")
        sys.exit()
    request_key = send_otp_res[1]
    
    # Verify TOTP
    print(f"Step 3: Verifying TOTP...\n Generated TOTP: {totp} \nRequest Key: {request_key} ")
    verify_totp_res = verify_totp(request_key, totp)
    if verify_totp_res[0] != SUCCESS:
        print(f"Failed to verify TOTP: {verify_totp_res[1]}")
        sys.exit()
    request_key_2 = verify_totp_res[1]
    
    # Verify PIN
    print("Step 4: Verifying PIN...")
    verify_pin_res = verify_pin(request_key_2, PIN)
    if verify_pin_res[0] != SUCCESS:
        print(f"Failed to verify PIN: {verify_pin_res[1]}")
        sys.exit()
    trade_access_token = verify_pin_res[1]
    
    # Get Auth Code
    print("Step 5: Retrieving Auth Code...")
    client_id = f"{APP_ID}-{APP_TYPE}"
    auth_code_res = get_auth_code(FY_ID, APP_ID, REDIRECT_URI, APP_TYPE, trade_access_token)
    if auth_code_res[0] != SUCCESS:
        print(f"Failed to get Auth Code: {auth_code_res[1]}")
        sys.exit()
    auth_code = auth_code_res[1]
    
    # Final Step: Generate Fyers API Access Token
    print("Step 6: Generating Final Access Token...")
    session = fyersModel.SessionModel(
        client_id=client_id, 
        secret_key=SECRET_KEY, 
        redirect_uri=REDIRECT_URI, 
        response_type='code', 
        grant_type='authorization_code'
    )
    
    session.set_token(auth_code)
    response = session.generate_token()
    
    if "access_token" in response:
        access_token = response["access_token"]
        print("\n✅ Authentication Successful!")
        print("-" * 50)
        print(f"ACCESS TOKEN: {access_token}")
        print("-" * 50)
        
        # Optional: Save to file for reuse
        with open("fyers_token.txt", "w") as f:
            f.write(access_token)
            
        return access_token
    else:
        print(f"❌ Failed to generate access token: {response}")
        sys.exit()

if __name__ == "__main__":
    token = get_final_access_token()
    
    # Test the token by fetching user profile
    if token:
        print("\nTesting Token with Fyers Profile API...")
        fyers = fyersModel.FyersModel(client_id=f"{APP_ID}-{APP_TYPE}", is_async=False, token=token, log_path="")
        profile = fyers.get_profile()
        print(json.dumps(profile, indent=2))