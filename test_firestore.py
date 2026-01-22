#!/usr/bin/env python3
"""
Test script to verify GCP service account credentials and Firestore access.
Run this inside the container to test if sa.json is working correctly.
"""

import os
import sys
import json
from datetime import datetime, timezone

def test_sa_json_file():
    """Test 1: Verify sa.json file exists and is valid JSON"""
    print("=" * 60)
    print("TEST 1: Checking sa.json file")
    print("=" * 60)
    
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        print("❌ GOOGLE_APPLICATION_CREDENTIALS environment variable not set")
        return False
    
    print(f"✅ GOOGLE_APPLICATION_CREDENTIALS = {creds_path}")
    
    if not os.path.exists(creds_path):
        print(f"❌ File does not exist: {creds_path}")
        return False
    
    print(f"✅ File exists: {creds_path}")
    
    # Check file size
    file_size = os.path.getsize(creds_path)
    print(f"✅ File size: {file_size} bytes")
    
    if file_size < 500:
        print("⚠️  Warning: File seems too small (should be ~1-3KB)")
    
    # Validate JSON
    try:
        with open(creds_path, 'r') as f:
            creds_data = json.load(f)
        
        print("✅ File is valid JSON")
        
        # Check required fields
        required_fields = ["type", "project_id", "private_key", "client_email"]
        missing_fields = [f for f in required_fields if f not in creds_data]
        
        if missing_fields:
            print(f"❌ Missing required fields: {missing_fields}")
            return False
        
        print("✅ All required fields present")
        print(f"   - type: {creds_data.get('type')}")
        print(f"   - project_id: {creds_data.get('project_id')}")
        print(f"   - client_email: {creds_data.get('client_email')}")
        
        # Check private_key
        private_key = creds_data.get('private_key', '')
        if not private_key.startswith('-----BEGIN PRIVATE KEY-----'):
            print("❌ private_key doesn't start with '-----BEGIN PRIVATE KEY-----'")
            return False
        
        if not private_key.endswith('-----END PRIVATE KEY-----\n'):
            print("⚠️  Warning: private_key doesn't end with '-----END PRIVATE KEY-----\\n'")
            print("   (This might be OK if newlines are escaped)")
        
        # Count newlines in private_key (should have many \n sequences)
        newline_count = private_key.count('\\n')
        if newline_count < 10:
            print(f"⚠️  Warning: private_key has only {newline_count} \\n sequences (expected 15-20+)")
        else:
            print(f"✅ private_key has {newline_count} \\n sequences (looks complete)")
        
        return True
        
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON: {e}")
        return False
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        return False


def test_firestore_client():
    """Test 2: Verify Firestore client can be created"""
    print("\n" + "=" * 60)
    print("TEST 2: Creating Firestore client")
    print("=" * 60)
    
    try:
        from google.cloud import firestore
        
        # Create client
        db = firestore.Client()
        print("✅ Firestore client created successfully")
        
        # Get project ID
        project_id = db.project
        print(f"✅ Connected to project: {project_id}")
        
        return db
        
    except Exception as e:
        print(f"❌ Failed to create Firestore client: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_firestore_read(db, collection_name="sessionContexts", device_id="90:e5:b1:a8:ac:90"):
    """Test 3: Test reading from Firestore"""
    print("\n" + "=" * 60)
    print(f"TEST 3: Reading from Firestore collection '{collection_name}'")
    print("=" * 60)
    
    if not db:
        print("❌ Skipping - Firestore client not available")
        return False
    
    try:
        # Test 1: List collections (requires read permission)
        print(f"\n📋 Testing: List collections...")
        collections = list(db.collections())
        print(f"✅ Can list collections: {len(collections)} found")
        for col in collections[:5]:  # Show first 5
            print(f"   - {col.id}")
        
        # Test 2: Read from sessionContexts
        print(f"\n📋 Testing: Read document '{device_id}' from '{collection_name}'...")
        doc_ref = db.collection(collection_name).document(device_id)
        doc = doc_ref.get()
        
        if doc.exists:
            print(f"✅ Document exists!")
            data = doc.to_dict()
            print(f"   - sessionType: {data.get('sessionType')}")
            print(f"   - triggeredAt: {data.get('triggeredAt')}")
            print(f"   - sessionConfig: {data.get('sessionConfig', {})}")
            return True
        else:
            print(f"⚠️  Document does not exist (this is OK if no alarm is scheduled)")
            return True  # Not an error - just no document
        
    except Exception as e:
        print(f"❌ Failed to read from Firestore: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_firestore_write(db, collection_name="sessionContexts", device_id="test_device_123"):
    """Test 4: Test writing to Firestore (optional - may not have write permission)"""
    print("\n" + "=" * 60)
    print(f"TEST 4: Writing to Firestore (testing write permissions)")
    print("=" * 60)
    
    if not db:
        print("❌ Skipping - Firestore client not available")
        return False
    
    try:
        print(f"\n📋 Testing: Write test document to '{collection_name}'...")
        test_doc_ref = db.collection(collection_name).document(device_id)
        
        test_data = {
            "test": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": "This is a test write"
        }
        
        test_doc_ref.set(test_data)
        print("✅ Write successful!")
        
        # Clean up - delete test document
        print(f"🧹 Cleaning up test document...")
        test_doc_ref.delete()
        print("✅ Test document deleted")
        
        return True
        
    except Exception as e:
        print(f"⚠️  Write test failed (may not have write permissions): {e}")
        print("   (This is OK if you only need read permissions)")
        return None  # Not a failure, just no write permission


def test_session_context_store(device_id="90:e5:b1:a8:ac:90"):
    """Test 5: Test using the actual session_context_store module"""
    print("\n" + "=" * 60)
    print("TEST 5: Testing session_context_store.get_session()")
    print("=" * 60)
    
    try:
        # Import the actual module
        sys.path.insert(0, '/opt/xiaozhi-esp32-server')
        from services.session_context import store as session_context_store
        
        print(f"📋 Testing: get_session('{device_id}')...")
        session = session_context_store.get_session(device_id)
        
        if session:
            print(f"✅ Session retrieved successfully!")
            print(f"   - session_type: {session.session_type}")
            print(f"   - device_id: {session.device_id}")
            print(f"   - session_config: {session.session_config}")
            return True
        else:
            print(f"⚠️  No session found (this is OK if no alarm is scheduled)")
            return True  # Not an error
        
    except Exception as e:
        print(f"❌ Failed to get session: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    print("\n" + "=" * 60)
    print("Firestore Credentials and Access Test")
    print("=" * 60)
    print()
    
    results = {}
    
    # Test 1: sa.json file
    results['sa_json'] = test_sa_json_file()
    
    # Test 2: Firestore client
    db = test_firestore_client()
    results['firestore_client'] = db is not None
    
    # Test 3: Read from Firestore
    if db:
        results['firestore_read'] = test_firestore_read(db)
        results['firestore_write'] = test_firestore_write(db)
    
    # Test 4: Session context store
    results['session_context'] = test_session_context_store()
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    for test_name, result in results.items():
        if result is True:
            print(f"✅ {test_name}: PASSED")
        elif result is False:
            print(f"❌ {test_name}: FAILED")
        else:
            print(f"⚠️  {test_name}: SKIPPED/NO PERMISSION")
    
    all_passed = all(r is True or r is None for r in results.values())
    
    if all_passed:
        print("\n✅ All critical tests passed!")
        return 0
    else:
        print("\n❌ Some tests failed - check output above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
