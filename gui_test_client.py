# // ================================================
# // gui_test_client.py (CLI GUI)
# // A command-line interface to interact with the ToDo Service layer for testing purposes.
# // ================================================

import uuid
from datetime import datetime, timedelta
import requests # <-- NEW: Need to use a library for HTTP calls

# Define the base URL of your running backend server
BASE_URL = "http://localhost:3000/api/v1/todos"
def display_menu():
    print("\n" + "="*50)
    print("           ✨ To-Do Manager CLI Test ✨")
    print("="*50)
    print("1. Create a Manual To-Do Task (Manual Source)")
    print("2. Simulate Calendar Sync (CALENDAR Source)")
    print("3. Simulate Email Sync (EMAIL Source)")
    print("4. Check for Overdue Tasks (Phase 3)")
    print("5. Exit")
    print("="*50)

def run_manual_task():
    print("\n--- [MANUAL CREATION MODE] ---")
    title = input("Enter Task Title: ")
    description = input("Enter Description (Optional): ").strip() or ""
    due_date_str = input("Enter Due Date (YYYY-MM-DD) [Leave blank for no date]: ")
    user_id = "fake-user-123" # Hardcoded for test
    
    due_date = None
    if due_date_str:
        try:
            # Simple formatting assumption for testing
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
        except ValueError:
            print("⚠️ Invalid date format. Using null due date.")

    try:
        payload = {
            "title": title,
            "description": description,
            "due_date": due_date_str,
            "source": "MANUAL" # Must explicitly set source for the API
        }
        response = requests.post(BASE_URL, json=payload)
        data = response.json()

        if response.status_code == 201:
            print(f"\n✅ SUCCESS! Task created and logged to service layer: {data['todo_id']}")
        else:
            print(f"❌ FAILED TO CREATE TASK (Status {response.status_code}): {data.get('message', 'Unknown error')}")

    except requests.exceptions.ConnectionError:
        print("\n🚨 CONNECTION ERROR: Could not connect to the backend server. Is Node.js running?")


def run_calendar_sync():
    print("\n--- [CALENDAR SYNC MODE] ---")
    # Simulate a Calendar Event payload from the API
    mock_event = {
        'summary': 'Quarterly Review Meeting',
        'location': 'Zoom Room A',
        'start': {'dateTime': (datetime.now() + timedelta(days=5)).isoformat()}, # 5 days from now
    }
    print("Simulating incoming calendar data...")
    try:
        print("⚠️ NOTE: Calendar Sync requires a new dedicated API endpoint on the Node.js server.")
        # Example placeholder for future implementation:
        # response = requests.post(BASE_URL + "/calendar", json=mock_event)
    except Exception as e:
        print(f"❌ CALENDAR SYNC FAILED: {e}")


def run_email_sync():
    print("\n--- [EMAIL SYNC MODE] ---")
    # Simulate an Email payload from the API
    mock_email = {
        'sender': 'client@company.com',
        'subject': 'Follow up on Q3 Report',
        'snippet': 'Please ensure all data points are validated before EOD Friday.',
    }
    print("Simulating incoming email data...")
    try:
        print("⚠️ NOTE: Email Sync requires a new dedicated API endpoint on the Node.js server.")

    except Exception as e:
        print(f"❌ EMAIL SYNC FAILED: {e}")


def run_overdue_check():
    print("\n--- [OVERDUE TASK CHECK] ---")
    try:
        response = requests.get(f"{BASE_URL}?status=PENDING&due_before={datetime.now().strftime('%Y-%m-%d')}")
        data = response.json()

        if data and 'message' in data:
            print("✅ SUCCESS! Overdue check simulated successfully.")
            print(f"Received status: {data['message']}")
        else:
             print("👍 All tasks are up to date! No immediate action required (or API returned no data).")

    except requests.exceptions.ConnectionError:
        print("\n🚨 CONNECTION ERROR: Could not connect to the backend server. Is Node.js running?")


def main():
    while True:
        display_menu()
        choice = input("Enter your choice (1-5): ").strip()

        if choice == '1':
            run_manual_task()
        elif choice == '2':
            run_calendar_sync()
        elif choice == '3':
            run_email_sync()
        elif choice == '4':
            run_overdue_check()
        elif choice == '5':
            print("Exiting To-Do Manager Test CLI.")
            break
        else:
            print("Invalid choice. Please select a number from the menu.")

if __name__ == "__main__":
    main()