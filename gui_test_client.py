// ================================================
// gui_test_client.py (CLI GUI)
// A command-line interface to interact with the ToDo Service layer for testing purposes.
// ================================================

import uuid
from datetime import datetime, timedelta
# Import the core service functions we just built!
from src.services.todoService import createTodo, syncFromCalendar, syncFromEmail, getOverdueTasks 

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
        new_task = createTodo({ 
            title=title, 
            description=description, 
            due_date=due_date, 
            user_id=user_id 
        });
        print(f"\n✅ SUCCESS! Task created and logged to service layer: {new_task['todo_id']}")
    except Exception as e:
        print(f"❌ FAILED TO CREATE TASK: {e}")


def run_calendar_sync():
    print("\n--- [CALENDAR SYNC MODE] ---")
    # Simulate a Calendar Event payload from the API
    mock_event = {
        'summary': 'Quarterly Review Meeting',
        'location': 'Zoom Room A',
        'start': {'dateTime': (datetime.now() + timedelta(days=5)).isoformat()}, # 5 days from now
        'user_id': "fake-user-123"
    }
    print("Simulating incoming calendar data...")
    try:
        task = syncFromCalendar(mock_event);
        print(f"\n✅ SYNC COMPLETE. Task status set to {task['status']} with source: {task['source']}")
    except Exception as e:
        print(f"❌ CALENDAR SYNC FAILED: {e}")


def run_email_sync():
    print("\n--- [EMAIL SYNC MODE] ---")
    # Simulate an Email payload from the API
    mock_email = {
        'sender': 'client@company.com',
        'subject': 'Follow up on Q3 Report',
        'snippet': 'Please ensure all data points are validated before EOD Friday.',
        'user_id': "fake-user-123"
    }
    print("Simulating incoming email data...")
    try:
        task = syncFromEmail(mock_email);
        print(f"\n✅ SYNC COMPLETE. Task status set to {task['status']} with source: {task['source']}")
    except Exception as e:
        print(f"❌ EMAIL SYNC FAILED: {e}")


def run_overdue_check():
    print("\n--- [OVERDUE TASK CHECK] ---")
    try:
        overdue = getOverdueTasks();
        if overdue:
            print("🚨 ATTENTION! The following tasks are OVERDUE or need attention:")
            # In a real GUI, we would format and display the list beautifully.
            for task in overdue:
                print(f"  - [{task['title']}] (Due: {task['due_date']})")
        else:
            print("👍 All tasks are up to date! No immediate action required.")

    except Exception as e:
        print(f"❌ OVERDUE CHECK FAILED: {e}")


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
    # Note: The service functions are now callable from this standalone script.
    main()