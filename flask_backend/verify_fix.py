#!/usr/bin/env python3
"""
Verification script to test that the Groq 'proxies' kwarg issue is fixed.
This script will attempt to call /summarize with a test URL and check for 502 errors.
"""
import requests
import json
import sys
import time

print("=" * 70)
print("Groq 'proxies' kwarg fix verification")
print("=" * 70)

# Wait for server to be ready
print("\n1. Checking if Flask server is running on port 3001...")
max_retries = 5
for i in range(max_retries):
    try:
        response = requests.get("http://localhost:3001/hello/", timeout=2)
        if response.status_code == 200:
            print(f"   ✓ Server is responding (status: {response.status_code})")
            break
    except requests.exceptions.ConnectionError:
        if i < max_retries - 1:
            print(f"   ⏳ Waiting for server... ({i+1}/{max_retries})")
            time.sleep(2)
        else:
            print("   ✗ Server is not responding on port 3001")
            print("   Please start the Flask server: python3 run.py")
            sys.exit(1)

# Test the /summarize endpoint with a minimal request
print("\n2. Testing /summarize endpoint...")
print("   Note: This test will use a YouTube URL. It may fail if:")
print("   - The video requires authentication (needs cookies)")
print("   - The video is too long (>5 minutes)")
print("   - Network connectivity issues")

# Use a known short public video
test_payload = {
    "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # First YouTube video, 18 seconds
}

print("\n   Sending POST to http://localhost:3001/summarize")
print(f"   Payload: {json.dumps(test_payload, indent=6)}")

try:
    response = requests.post(
        "http://localhost:3001/summarize",
        json=test_payload,
        timeout=120  # Give it 2 minutes
    )
    
    print("\n3. Response received:")
    print(f"   Status Code: {response.status_code}")
    
    try:
        response_json = response.json()
        print("   Response Body:")
        print(json.dumps(response_json, indent=6))
    except:
        print("   Response Body (raw):")
        print(f"   {response.text[:500]}")
    
    # Check for the specific error
    if response.status_code == 502:
        print("\n" + "=" * 70)
        print("✗ FAILED: Still getting 502 Bad Gateway")
        print("=" * 70)
        
        if "proxies" in response.text.lower():
            print("\n⚠ The response contains 'proxies' - the issue persists!")
            print("\nPossible causes:")
            print("  1. Old bytecode cache not cleared")
            print("  2. Server not restarted with new code")
            print("  3. Middleware or decorator wrapping Groq()")
            sys.exit(1)
        else:
            print("\n⚠ 502 error but not related to 'proxies' kwarg")
            sys.exit(1)
    
    elif response.status_code == 200:
        print("\n" + "=" * 70)
        print("✓ SUCCESS: /summarize endpoint returned 200 OK!")
        print("=" * 70)
        
        # Verify response structure
        if "summary" in response_json and "recommended_title" in response_json:
            print("\n✓ Response contains expected fields (summary, recommended_title)")
            print("\n🎉 The Groq 'proxies' kwarg issue is RESOLVED!")
            sys.exit(0)
        else:
            print("\n⚠ Response is 200 but missing expected fields")
            sys.exit(1)
    
    elif response.status_code in [400, 401, 403]:
        print("\n" + "=" * 70)
        print(f"⚠ Client error ({response.status_code})")
        print("=" * 70)
        print("\nThis might be expected if the video requires authentication")
        print("or if there are other validation issues.")
        print("\nThe important thing is that we're NOT getting a 502 with 'proxies' error.")
        
        if "proxies" not in response.text.lower():
            print("\n✓ No 'proxies' kwarg error detected - the fix appears to be working!")
            sys.exit(0)
        else:
            print("\n✗ Still seeing 'proxies' in error response")
            sys.exit(1)
    
    else:
        print("\n" + "=" * 70)
        print(f"⚠ Unexpected status code: {response.status_code}")
        print("=" * 70)
        
        if "proxies" not in response.text.lower():
            print("\n✓ No 'proxies' kwarg error detected")
        else:
            print("\n✗ 'proxies' kwarg error still present")
            sys.exit(1)
        
except requests.exceptions.Timeout:
    print("\n✗ Request timed out after 120 seconds")
    print("   This suggests the endpoint is hanging or taking too long")
    sys.exit(1)
    
except Exception as e:
    print(f"\n✗ Unexpected error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
