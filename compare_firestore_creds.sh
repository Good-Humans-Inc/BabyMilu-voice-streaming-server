#!/bin/bash
# Script to compare Firestore credentials between staging and bm-staging-vm
# Run this on both VMs to identify differences

echo "=========================================="
echo "Firestore Credentials Comparison"
echo "=========================================="
echo

echo "1. Checking file existence and permissions:"
echo "-------------------------------------------"
if [ -f "/run/secrets/gcp_sa.json" ]; then
    echo "✅ File exists: /run/secrets/gcp_sa.json"
    ls -lh /run/secrets/gcp_sa.json
    echo "   Size: $(stat -f%z /run/secrets/gcp_sa.json 2>/dev/null || stat -c%s /run/secrets/gcp_sa.json) bytes"
else
    echo "❌ File does NOT exist: /run/secrets/gcp_sa.json"
fi
echo

echo "2. Checking source file on host:"
echo "-------------------------------------------"
if [ -f "/srv/staging/current/data/.gcp/sa.json" ]; then
    echo "✅ Source file exists: /srv/staging/current/data/.gcp/sa.json"
    ls -lh /srv/staging/current/data/.gcp/sa.json
    echo "   Size: $(stat -f%z /srv/staging/current/data/.gcp/sa.json 2>/dev/null || stat -c%s /srv/staging/current/data/.gcp/sa.json) bytes"
    
    # Calculate checksum
    if command -v md5sum &> /dev/null; then
        echo "   MD5: $(md5sum /srv/staging/current/data/.gcp/sa.json | cut -d' ' -f1)"
    elif command -v md5 &> /dev/null; then
        echo "   MD5: $(md5 -q /srv/staging/current/data/.gcp/sa.json)"
    fi
else
    echo "❌ Source file does NOT exist: /srv/staging/current/data/.gcp/sa.json"
fi
echo

echo "3. Validating JSON structure:"
echo "-------------------------------------------"
if [ -f "/run/secrets/gcp_sa.json" ]; then
    if python3 -m json.tool /run/secrets/gcp_sa.json > /dev/null 2>&1; then
        echo "✅ JSON is valid"
        
        # Check required fields
        python3 << 'PYEOF'
import json
import sys

try:
    with open('/run/secrets/gcp_sa.json', 'r') as f:
        creds = json.load(f)
    
    required = ['type', 'project_id', 'private_key', 'client_email']
    missing = [f for f in required if f not in creds]
    
    if missing:
        print(f"❌ Missing fields: {missing}")
        sys.exit(1)
    
    print("✅ All required fields present")
    print(f"   - type: {creds.get('type')}")
    print(f"   - project_id: {creds.get('project_id')}")
    print(f"   - client_email: {creds.get('client_email')}")
    
    # Check private_key
    pk = creds.get('private_key', '')
    if not pk.startswith('-----BEGIN PRIVATE KEY-----'):
        print("❌ private_key doesn't start correctly")
        sys.exit(1)
    
    if not pk.endswith('-----END PRIVATE KEY-----\n'):
        print("⚠️  private_key doesn't end correctly (might be OK if newlines escaped)")
    
    # Count newline sequences
    nl_count = pk.count('\\n')
    print(f"   - private_key has {nl_count} \\n sequences")
    
    if nl_count < 10:
        print("⚠️  Warning: private_key seems incomplete (expected 15-20+ newlines)")
    else:
        print("✅ private_key appears complete")
    
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)
PYEOF
    else
        echo "❌ JSON is invalid"
    fi
fi
echo

echo "4. Testing Firestore client creation:"
echo "-------------------------------------------"
python3 << 'PYEOF'
import os
import sys

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/run/secrets/gcp_sa.json'

try:
    from google.cloud import firestore
    db = firestore.Client()
    print(f"✅ Firestore client created")
    print(f"   Project: {db.project}")
except Exception as e:
    print(f"❌ Failed to create client: {e}")
    sys.exit(1)
PYEOF
echo

echo "5. Testing Firestore read (with timeout):"
echo "-------------------------------------------"
python3 << 'PYEOF'
import os
import sys
import signal

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/run/secrets/gcp_sa.json'

def timeout_handler(signum, frame):
    raise TimeoutError("Operation timed out after 10 seconds")

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(10)  # 10 second timeout

try:
    from google.cloud import firestore
    db = firestore.Client()
    doc = db.collection('sessionContexts').document('90:e5:b1:a8:ac:90').get(timeout=5.0)
    signal.alarm(0)  # Cancel timeout
    
    if doc.exists:
        print("✅ Document exists")
        data = doc.to_dict()
        print(f"   sessionType: {data.get('sessionType')}")
    else:
        print("⚠️  Document does not exist (OK if no alarm scheduled)")
except TimeoutError:
    signal.alarm(0)
    print("❌ Operation timed out (Firestore is unreachable or credentials invalid)")
except Exception as e:
    signal.alarm(0)
    print(f"❌ Error: {e}")
    if "Invalid JWT Signature" in str(e):
        print("   ⚠️  This means the private key is corrupted or invalid!")
PYEOF
echo

echo "6. Environment variable check:"
echo "-------------------------------------------"
if [ -n "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
    echo "✅ GOOGLE_APPLICATION_CREDENTIALS is set: $GOOGLE_APPLICATION_CREDENTIALS"
else
    echo "⚠️  GOOGLE_APPLICATION_CREDENTIALS is not set in shell"
fi

# Check in container
echo "   In container:"
docker exec current-server-1 env 2>/dev/null | grep GOOGLE_APPLICATION_CREDENTIALS || echo "   (container not running or env not set)"
echo

echo "=========================================="
echo "Comparison complete!"
echo "=========================================="
echo
echo "Next steps:"
echo "1. Run this script on BOTH staging and bm-staging-vm"
echo "2. Compare the outputs, especially:"
echo "   - File sizes and checksums"
echo "   - private_key newline counts"
echo "   - Firestore read results"
echo "3. If checksums differ, the files are different - copy the working one"
