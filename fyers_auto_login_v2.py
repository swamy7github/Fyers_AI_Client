import json
import base64
import requests
import pyotp
from urllib.parse import parse_qs, urlparse
from fyers_apiv3 import fyersModel
import time
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
SECRET_KEY = "HJ5RUXDPW2"   #HJ5RUXDPW2

#===================================================
APP_TYPE = "100"             # Usually '100'
FY_ID = "YY01479"      # Example: 'XY12345'
TOTP_KEY = "U6DLWH4CW64MFZJV4KOOI6F47WG55U4W" # 32-character base32 secret from Fyers account settings
PIN = "1436"      # e.g., '1234'
REDIRECT_URI = "http://127.0.0.1:5000/fyers/ai_callback/" # Example: 'https://trade.fyers.in/api-login/redirect-uri/index.html'

def encode_base64(input_string):
    """Encodes a string to base64 required by Vagator v2 endpoints."""
    return base64.b64encode(str(input_string).encode('ascii')).decode('ascii')

def get_final_access_token():
    """Main workflow to generate the final Fyers API V3 Access Token."""
    print("Starting Fyers Auto-Login Process...")
    
    # CRITICAL: We must use a Session object to persist cookies across all Vagator API calls.
    # Without this, Step 4 will fail with the 'must use the fyers APIv3 APIs' error.
    session = requests.Session()
    
    try:
        # Step 1: Send Login OTP Request
        print("Step 1: Sending Login Request...")
        url_send_otp = "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2"
        payload_otp = {
            "fy_id": encode_base64(FY_ID),
            "app_id": "2" # Strictly "2" for web simulation
        }
        res1 = session.post(url=url_send_otp, json=payload_otp)
        print(f"Login Request Response: {res1.status_code} - {res1.text}")
        if res1.status_code != 200:
            print(f"Failed to send login request: {res1.text}")
            return None
        request_key = res1.json()["request_key"]
        print(f"Login Request Sent. Request Key: {request_key}")
        
        # Step 2: Verify TOTP
        print("Step 2: Verifying TOTP...")
        # Optional safeguard: wait if the TOTP is about to expire (seconds close to 30 or 60)
        # to ensure it doesn't expire in flight.
        if time.localtime().tm_sec % 30 > 27:
            time.sleep(5)
            
        totp_code = pyotp.TOTP(TOTP_KEY).now()
        url_verify_totp = "https://api-t2.fyers.in/vagator/v2/verify_otp"
        payload_totp = {
            "request_key": request_key,
            "otp": totp_code
        }
        res2 = session.post(url=url_verify_totp, json=payload_totp)
        print(f"TOTP Verification Response: {res2.status_code} - {res2.text}")
        if res2.status_code != 200:
            print(f"Failed to verify TOTP: {res2.text}")
            return None
        request_key_2 = res2.json()["request_key"]
        
        # Step 3: Verify PIN
        print("Step 3: Verifying PIN...")
        url_verify_pin = "https://api-t2.fyers.in/vagator/v2/verify_pin_v2"
        payload_pin = {
            "request_key": request_key_2,
            "identity_type": "pin",
            "identifier": encode_base64(PIN)
        }
        res3 = session.post(url=url_verify_pin, json=payload_pin)
        print(f"PIN Verification Response: {res3.status_code} - {res3.text}")
        if res3.status_code != 200:
            print(f"Failed to verify PIN:{res3.status_code} - {res3.text}")
            return None
        trade_access_token = res3.json()['data']['access_token']
        trade_refresh_token = res3.json()['data']['refresh_token']        
        # Step 4: Add Bearer token to Session headers and Get Auth Code
        print("Step 4: Retrieving Auth Code...")
        session.headers.update({
            # 'Authorization': f"{trade_access_token}",
            # 'Refresh-token': f"{trade_refresh_token}"
            'Authorization': f"{trade_access_token}",
            'Content-Type': 'application/json'
        })
        
        print(f"Session Headers Updated with Bearer Token: {session.headers}")
        
        url_token = "https://api-t1.fyers.in/gk/verify_token"
                
        res_get=session.get(url=url_token, headers=session.headers)
        print(f"Token Verify Response: {res_get.status_code} - {res_get.text}")
        if res_get.status_code != 200:
            print(f"Failed to Verify tokens: {res_get.status_code} - {res_get.text}")
            return None
                
        url_token = "https://api-t1.fyers.in/gk/validate_tokens"
        
        res_get=session.get(url=url_token, headers=session.headers)
        print(f"Token Validation Response: {res_get.status_code} - {res_get.text}")
        if res_get.status_code != 200:
            print(f"Failed to validate tokens: {res_get.status_code} - {res_get.text}")
            return None
        return trade_access_token
        '''
        url_token = "https://api-t1.fyers.in/api/v3/profile"
        headers = {
            "Authorization": f"{APP_ID}-{APP_TYPE}:{trade_access_token}",
            "Content-Type": "application/json"}
        res_get = session.get(url=url_token, headers=headers)
        print(f"Profile Response: {res_get.status_code} - {res_get.text}")
        if res_get.status_code != 200:
            print(f"Failed to validate tokens: {res_get.status_code} - {res_get.text}")
            return None
        print(f"Returining Profile Response: {res_get.status_code} - {res_get.text}")
        return 
    
        url_token = "https://api-t1.fyers.in/api/v3/token"
        
        payload_auth_code = {
            "fyers_id": FY_ID,
            "app_id": APP_ID, # Pass clean APP_ID without the '-100' here
            "redirect_uri": REDIRECT_URI,
            "appType": APP_TYPE,
            "code_challenge": "",
            "state": "None",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True
        }
        res4 = session.post(url=url_token, json=payload_auth_code, headers=session.headers)
        print(f"Auth Code Request Response: {res4.status_code} - {res4.text}")
        res4_data = res4.json()
        print(f"Auth Code Response Data: {json.dumps(res4_data, indent=2)}")
        
        # Gracefully handle API errors masked inside a 200 OK response
        if "s" in res4_data and res4_data["s"] == "error":
            print(f"API Error during Auth Code retrieval: {res4_data}")
            return None
          
        url_redirect = res4_data['data']['redirectUrl']
        if not url_redirect:
            print(f"Failed to extract URL from response: {res4_data}")
            return None
             
        #parsed = urlparse(url_redirect)
        #auth_code = parse_qs(parsed.query)['auth_code'][0]
        
        result_auth = res4_data["data"]["auth"]
        auth_code = result_auth #result_auth.rsplit(".", 1)[0]
        print(f"auth_code : {auth_code}")
        # Step 5: Exchange Auth Code for Final API V3 Access Token
        print("Step 5: Generating Final Access Token...")
        fyers_session = fyersModel.SessionModel(
            client_id=f"{APP_ID}-{APP_TYPE}",
            secret_key=SECRET_KEY,
            redirect_uri=REDIRECT_URI,
            response_type="code",
            grant_type="authorization_code"
        )
        print(f"Setting auth_code in Fyers Session: {fyers_session}")
        print(f"DIR : {dir(fyers_session)}")
        #fyers_session.set_token(auth_code)
        #response = fyers_session.generate_token()
        #print(f"Final Access Token Response: {response}")
        return auth_code
        if "access_token" in response:
            final_token = response["access_token"]
            print("\n✅ Authentication Successful!")
            print("-" * 50)
            print(f"ACCESS TOKEN: {final_token}")
            print("-" * 50)
            
            # Optional: Save to file for reuse in your trading bots
            with open("fyers_token.txt", "w") as f:
                f.write(final_token)
                
            return final_token
        else:
            print(f"❌ Failed to generate access token: {response}")
            return None
'''
    except Exception as e:
        print(f"An exception occurred during authentication: {e}")
        return None

if __name__ == "__main__":
    token = get_final_access_token()
    
    if token:
        print("\nTesting Token with Fyers Profile API...")
        fyers = fyersModel.FyersModel(client_id=f"{APP_ID}-{APP_TYPE}", token=token, is_async=False, log_path="")
        profile = fyers.get_profile()
        url = "https://api-t1.fyers.in/trade/v3/positions"

        headers = {
            "Authorization": f"{APP_ID}-{APP_TYPE}:{token}",
            "content-type": "application/json"
}

        #r = requests.get(url, headers=headers)

        #print("STATUS:", r.status_code)
        #print("RESPONSE:", r.json())
        print(json.dumps(profile, indent=2))