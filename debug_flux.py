#!/usr/bin/env python3
"""Debug script to test FLUX API and see exact response."""

import os
import requests
import json

API_KEY = os.getenv('FLUX_API_KEY', 'YOUR_KEY_HERE')
BASE_URL = "https://api.bfl.ai/v1"

# Simple test prompt
payload = {
    "prompt": "A red apple on a white background",
    "width": 1024,
    "height": 1024,
    "num_images": 1,
    "safety_tolerance": 3,
    "output_format": "png",
    "prompt_upsampling": True,
}

headers = {
    "X-Key": API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

print("=" * 60)
print("FLUX API Debug Test")
print("=" * 60)

# Create request
url = f"{BASE_URL}/flux-2-pro"
print(f"\nPOST {url}")
print(f"Payload: {json.dumps(payload, indent=2)}")

try:
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"\nResponse Status: {response.status_code}")
    print(f"Response Headers: {dict(response.headers)}")
    
    data = response.json()
    print(f"\nResponse Body:")
    print(json.dumps(data, indent=2))
    
    # Check for various fields
    print("\n" + "=" * 60)
    print("Field Analysis:")
    print("=" * 60)
    
    if 'id' in data:
        print(f"✓ id: {data['id']}")
    if 'task_id' in data:
        print(f"✓ task_id: {data['task_id']}")
    if 'polling_url' in data:
        print(f"✓ polling_url: {data['polling_url']}")
    if 'status' in data:
        print(f"✓ status: {data['status']}")
    if 'result' in data:
        print(f"✓ result: {data['result']}")
        if isinstance(data['result'], dict):
            if 'sample' in data['result']:
                print(f"  ✓ result.sample: {data['result']['sample']}")
    if 'sample' in data:
        print(f"✓ sample: {data['sample']}")
    if 'url' in data:
        print(f"✓ url: {data['url']}")
        
    # If we have a polling_url, try to poll
    polling_url = data.get('polling_url') or data.get('pollingUrl')
    task_id = data.get('id') or data.get('task_id')
    
    if polling_url and task_id:
        print(f"\n" + "=" * 60)
        print("Polling for result...")
        print("=" * 60)
        
        for i in range(10):
            import time
            time.sleep(1)
            
            poll_response = requests.get(
                polling_url,
                headers=headers,
                params={'id': task_id},
                timeout=30
            )
            poll_data = poll_response.json()
            status = poll_data.get('status')
            print(f"Poll {i+1}: status={status}")
            
            if status == 'Ready':
                print(f"\n✓ Image ready!")
                if 'result' in poll_data and 'sample' in poll_data['result']:
                    print(f"URL: {poll_data['result']['sample']}")
                break
            elif status in ['Error', 'Failed']:
                print(f"\n✗ Generation failed: {poll_data}")
                break
                
except Exception as e:
    print(f"\n✗ Error: {e}")
    import traceback
    traceback.print_exc()
