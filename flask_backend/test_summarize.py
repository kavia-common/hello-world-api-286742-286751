#!/usr/bin/env python3
"""
Test script to verify /summarize endpoint works without proxy errors.
"""
import requests
import json

# Test with a very short video URL (adjust as needed)
# Using a short public domain video
test_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo" - first YouTube video (18 seconds)

print("=" * 60)
print("Testing /summarize endpoint")
print("=" * 60)

payload = {
    "url": test_url
}

print("\nSending POST request to http://localhost:3001/summarize")
print(f"Payload: {json.dumps(payload, indent=2)}")

try:
    response = requests.post(
        "http://localhost:3001/summarize",
        json=payload,
        timeout=60
    )
    
    print(f"\nResponse Status Code: {response.status_code}")
    print(f"Response Headers: {dict(response.headers)}")
    print("\nResponse Body:")
    
    try:
        response_json = response.json()
        print(json.dumps(response_json, indent=2))
        
        if response.status_code == 200:
            print("\n" + "=" * 60)
            print("✓ SUCCESS: /summarize endpoint working correctly!")
            print("=" * 60)
            
            # Verify expected fields
            if "summary" in response_json and "recommended_title" in response_json and "full_transcript" in response_json:
                print("\n✓ All expected fields present in response")
            else:
                print("\n⚠ WARNING: Some expected fields missing from response")
        elif response.status_code == 502:
            print("\n" + "=" * 60)
            print("✗ FAILED: 502 Bad Gateway - Groq proxy error likely still present")
            print("=" * 60)
        else:
            print("\n" + "=" * 60)
            print(f"⚠ Unexpected status code: {response.status_code}")
            print("=" * 60)
            
    except json.JSONDecodeError:
        print(response.text)
        
except requests.exceptions.Timeout:
    print("\n✗ Request timed out after 60 seconds")
    print("This might indicate the endpoint is hanging")
    
except requests.exceptions.ConnectionError as e:
    print(f"\n✗ Connection error: {e}")
    print("Make sure the Flask server is running on port 3001")
    
except Exception as e:
    print(f"\n✗ Unexpected error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
