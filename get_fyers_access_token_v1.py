import json
import requests
import pyotp
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel

# --- CONFIGURATION ---
FY_ID = "YY01479"        # e.g., "XY12345"
PIN = "1436"              # Your Fyers login PIN
TOTP_SECRET = "U6DLWH4CW64MFZJV4KOOI6F47WG55U4W"  # Alphanumeric secret key from Fyers QR page

APP_ID = "YFLRWVHU2G-100"                # App ID from myapi.fyers.in (exclude the -100)
SECRET_KEY = "HJ5RUXDPW2"        # App Secret Key from myapi.fyers.in
REDIRECT_URI = "http://127.0.0.1:5000/fyers/ai_callback"    # Must match your app config exactly
# ---------------------

def get_automated_token():
    # 1. Initialize Fyers v3 Session
    # Format client_id as 'appId-appType' where web app type is '100'
    client_id = f"{APP_ID}-100" 
    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )
    
    # 2. Step 1: Send Client ID to Fyers Login System
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    r1 = requests.post(
        url='https://fyers.in',
        headers=headers,
        json={"fy_id": FY_ID, "app_id_type": "2"}
    )
    print(f"Step 1 Response: {r1}")
    request_key = r1.get('request_key')
    if not request_key:
        raise Exception(f"Failed at step 1 (Validate Client ID): {r1}")

    # 3. Step 2: Generate TOTP & Submit for Second-Factor Auth
    totp = pyotp.TOTP(TOTP_SECRET).now()
    print(f"Generated TOTP: {totp} (valid for 30 seconds)")
    r2 = requests.post(
        url='https://fyers.in',
        headers=headers,
        json={"request_key": request_key, "totp": totp}
    ).json()
    
    request_key = r2.get('request_key')
    if not request_key:
        raise Exception(f"Failed at step 2 (Validate TOTP): {r2}")

    # 4. Step 3: Validate Account PIN
    r3 = requests.post(
        url='https://fyers.in',
        headers=headers,
        json={"request_key": request_key, "pin": PIN}
    ).json()
    
    auth_code_token = r3.get('auth_code')
    if not auth_code_token:
        raise Exception(f"Failed at step 3 (Validate PIN): {r3}")

    # 5. Step 4: Request the final Authorization Redirect Code
    # This simulates clicking the app authentication redirect
    payload = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": "sample_state",
        "auth_code": auth_code_token
    }
    
    r4 = requests.post(
        url='https://fyers.in',
        headers=headers,
        json=payload
    ).json()
    
    # Extract the authorization code from the redirect URL target
    redirect_url = r4.get('url')
    if not redirect_url:
        raise Exception(f"Failed at step 4 (Get redirect URL): {r4}")
        
    parsed_url = urlparse(redirect_url)
    auth_code = parse_qs(parsed_url.query).get('auth_code')[0]

    # 6. Step 5: Exchange the code for the final Access Token via official SDK
    session.set_token(auth_code)
    token_response = session.generate_token()
    
    if token_response.get("s") == "ok":
        print("🎉 Access Token Generated Successfully!")
        return token_response.get("access_token")
    else:
        raise Exception(f"Failed to generate final access token: {token_response}")

if __name__ == "__main__":
    access_token = get_automated_token()
    print(f"Your Access Token:\n{access_token}")
