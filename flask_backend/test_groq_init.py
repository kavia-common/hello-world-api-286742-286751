#!/usr/bin/env python3
"""
Minimal test script to verify Groq client initialization without proxies.
This helps diagnose the 502 error related to unexpected 'proxies' kwarg.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
dotenv_path = Path(__file__).resolve().parent / '.env'
load_dotenv(dotenv_path=dotenv_path)

print("=" * 60)
print("Groq Client Initialization Test")
print("=" * 60)

# Check GROQ_API_KEY
groq_api_key = os.getenv("GROQ_API_KEY")
if groq_api_key:
    print(f"✓ GROQ_API_KEY found: {groq_api_key[:8]}...{groq_api_key[-4:]}")
else:
    print("✗ GROQ_API_KEY not found in environment!")
    sys.exit(1)

# Check for proxy environment variables
proxy_vars = ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]
found_proxies = []
for var in proxy_vars:
    val = os.getenv(var)
    if val:
        found_proxies.append(f"{var}={val}")

if found_proxies:
    print("\n⚠ WARNING: Proxy environment variables detected:")
    for p in found_proxies:
        print(f"  - {p}")
else:
    print("\n✓ No proxy environment variables found")

# Test Groq client initialization
print("\n" + "-" * 60)
print("Testing Groq() initialization with NO arguments...")
print("-" * 60)

try:
    from groq import Groq
    import inspect
    
    # Show the signature
    sig = inspect.signature(Groq.__init__)
    print(f"\nGroq.__init__ signature:\n  {sig}")
    
    # Verify 'proxies' is NOT in the signature
    params = list(sig.parameters.keys())
    if 'proxies' in params:
        print("\n✗ ERROR: 'proxies' parameter found in Groq.__init__ signature!")
    else:
        print("\n✓ Confirmed: 'proxies' is NOT a valid parameter for Groq.__init__")
    
    # Attempt instantiation
    print("\nInstantiating client = Groq()...")
    client = Groq()
    print(f"✓ SUCCESS: Groq client created: {type(client).__name__}")
    print(f"  - Client base_url: {getattr(client, 'base_url', 'N/A')}")
    print(f"  - Client api_key: {'***set***' if getattr(client, 'api_key', None) else 'NOT SET'}")
    
    # Check if client has an http_client
    http_client = getattr(client, '_client', None)
    if http_client:
        print(f"  - HTTP client type: {type(http_client).__name__}")
    
    print("\n" + "=" * 60)
    print("✓ All tests passed! Groq client can be initialized correctly.")
    print("=" * 60)
    
except TypeError as e:
    print(f"\n✗ FAILED with TypeError: {e}")
    print("\nThis error suggests a 'proxies' kwarg is being passed somewhere.")
    print("Check for:")
    print("  1. Monkey-patched Groq class")
    print("  2. Wrapper functions passing extra kwargs")
    print("  3. Cached bytecode with old code")
    sys.exit(1)
    
except Exception as e:
    print(f"\n✗ FAILED with unexpected error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
