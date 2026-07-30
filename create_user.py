import urllib.request
import json
import sys

# 1. Register
req_reg = urllib.request.Request(
    'http://localhost:8000/api/auth/register',
    data=json.dumps({"email": "badrmotii49@gmail.com", "password": "Password123!", "full_name": "Badr Motii"}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
    method='POST'
)

try:
    with urllib.request.urlopen(req_reg) as f:
        res = json.loads(f.read().decode('utf-8'))
        token = res.get("access_token")
        print("User registered successfully. Token obtained.")
except urllib.error.HTTPError as e:
    err = e.read().decode('utf-8')
    print(f"Error registering: {e.code} {err}")
    # If conflict, we can just login to get the token
    if e.code == 409:
        print("User already exists. Attempting login...")
        req_login = urllib.request.Request(
            'http://localhost:8000/api/auth/login',
            data=json.dumps({"email": "badrmotii49@gmail.com", "password": "Password123!"}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req_login) as flog:
            res = json.loads(flog.read().decode('utf-8'))
            token = res.get("access_token")
            print("Login successful. Token obtained.")
    else:
        sys.exit(1)

# 2. Upgrade Plan to 'elite' (all features except 'enterprise' which is admin/white-label)
# The user wants "all features except admin". Looking at FEATURE_MIN_PLAN:
# - elite grants api_access, copy_trading, auto_execution
# - enterprise grants white_label, sla_priority
# So 'elite' is perfect for "all features except admin".
req_upg = urllib.request.Request(
    'http://localhost:8000/api/billing/checkout/elite',
    data=b'',
    headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    },
    method='POST'
)

try:
    with urllib.request.urlopen(req_upg) as f:
        res = json.loads(f.read().decode('utf-8'))
        print("Plan upgraded successfully:", res)
except urllib.error.HTTPError as e:
    err = e.read().decode('utf-8')
    print(f"Error upgrading plan: {e.code} {err}")
    sys.exit(1)
