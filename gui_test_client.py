import tkinter as tk
from tkinter import messagebox
import requests
import uuid

# --- Configuration ---
# IMPORTANT: Change this URL to point to your running backend server endpoint
BASE_URL = "http://localhost:3000/api/v1/todos" 
MOCK_USER_ID = 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11' # Matches the mock in todoRoutes.js

class TodoGUI:
    def __init__(self, master):
        self.master = master
        master.title("Agenda ToDo Test Client")

        # --- 1. Task Display Area (Listbox) ---
        tk.Label(master, text="Your Tasks:", font=('Arial', 12)).pack(pady=5)
        self.task_listbox = tk.Listbox(master, height=15, width=80)
        self.task_listbox.pack(padx=10, pady=5)
        self.task_listbox.bind('<<ListboxSelect>>', self.load_selected_task_to_form)

        # --- 2. Input Form Frame ---
        form_frame = tk.Frame(master, padx=10, pady=10, bd=2, relief=tk.GROOVE)
        form_frame.pack(padx=10, pady=10, fill='x')

        # Labels and Entries setup (Simplified for brevity)
        tk.Label(form_frame, text="Title:").grid(row=0, column=0, sticky='w', padx=5, pady=2)
        self.title_entry = tk.Entry(form_frame, width=50)
        self.title_entry.grid(row=0, column=1, padx=5, pady=2)

        tk.Label(form_frame, text="Description:").grid(row=1, column=0, sticky='nw', padx=5, pady=2)
        self.desc_text = tk.Text(form_frame, height=4, width=30)
        self.desc_text.grid(row=1, column=1, padx=5, pady=2)

        tk.Label(form_frame, text="Due Date (YYYY-MM-DD):").grid(row=2, column=0, sticky='w', padx=5, pady=2)
        self.date_entry = tk.Entry(form_frame, width=20)
        self.date_entry.grid(row=2, column=1, sticky='w', padx=5, pady=2)

        tk.Label(form_frame, text="Source:").grid(row=3, column=0, sticky='w', padx=5, pady=2)
        self.source_var = tk.StringVar(value="MANUAL")
        self.source_menu = tk.OptionMenu(form_frame, self.source_var, "MANUAL", "CALENDAR", "EMAIL").grid(row=3, column=1, sticky='w', padx=5, pady=2)

        # --- 3. Buttons ---
        button_frame = tk.Frame(master, pady=10)
        button_frame.pack()

        self.add_button = tk.Button(button_frame, text="Create/Update Task", command=self.submit_task)
        self.add_button.grid(row=0, column=0, padx=10)

        self.refresh_button = tk.Button(button_frame, text="Refresh List", command=self.fetch_and_display_todos)
        self.refresh_button.grid(row=0, column=1, padx=10)

        # Initialize the view
        self.fetch_and_display_todos()


    def fetch_and_display_todos(self):
        """Fetches all todos from the backend and populates the Listbox."""
        try:
            response = requests.get(f"{BASE_URL}", headers={"Authorization": f"Bearer {MOCK_USER_ID}"})
            response.raise_for_status()
            todos = response.json()
            
            self.task_listbox.delete(0, tk.END)
            if not todos:
                self.task_listbox.insert(tk.END, "No tasks found.")
                return

            for todo in todos:
                display_text = f"[{todo['source']}] {todo['title']} (Due: {todo['due_date'] or 'N/A'})"
                self.task_listbox.insert(tk.END, display_text)
        except requests.exceptions.ConnectionError:
            messagebox.showerror("Connection Error", "Could not connect to the backend server. Ensure your API is running at " + BASE_URL)
        except Exception as e:
            messagebox.showerror("Error", f"An error occurred while fetching tasks: {e}")

    def load_selected_task_to_form(self, event):
        """Loads the details of the selected task from the Listbox into the form fields."""
        try:
            selection = self.task_listbox.curselection()
            if not selection:
                return
            
            # In a real app, you'd map the listbox index back to the actual data object/ID.
            # For this mock, we just clear fields and show a prompt.
            messagebox.showinfo("Selection", "Selected task loaded (In a full implementation, details would populate here).")

        except Exception as e:
            print(f"Error loading selection: {e}")


    def submit_task(self):
        """Handles the logic for creating or updating a ToDo item."""
        title = self.title_entry.get()
        description = self.desc_text.get("1.0", tk.END).strip()
        due_date = self.date_entry.get()
        source = self.source_var.get()

        if not title:
            messagebox.showwarning("Validation Error", "Title cannot be empty.")
            return

        # --- Determine if we are creating or updating (Mock logic) ---
        # In a real app, you'd check if an ID was pre-filled/selected.
        is_updating = False # Assume creation for this simple mock example
        todo_id = "mock_uuid" 

        payload = {
            "title": title,
            "description": description,
            "due_date": due_date if due_date else None,
            "status": "PENDING", # Default status for new items
            "source": source
        }

        try:
            if is_updating:
                url = f"{BASE_URL}/{todo_id}"
                response = requests.put(url, json=payload, headers={"Authorization": f"Bearer {MOCK_USER_ID}"})
            else:
                # POST request for creation
                response = requests.post(f"{BASE_URL}", json=payload, headers={"Authorization": f"Bearer {MOCK_USER_ID}"})

            response.raise_for_status()
            messagebox.showinfo("Success", "Task saved successfully! Refreshing list...")
            self.fetch_and_display_todos() # Refresh the view after successful operation

        except requests.exceptions.ConnectionError:
            messagebox.showerror("Connection Error", f"Could not connect to the backend server at {BASE_URL}. Is it running?")
        except requests.exceptions.HTTPError as e:
            try:
                error_details = response.json()
                messagebox.showerror("API Error", f"Failed to save task (Status {response.status_code}):\n{str(error_details)}")
            except:
                 messagebox.showerror("API Error", f"Failed to save task (Status {response.status_code}). Check server logs.")


if __name__ == "__main__":
    # Ensure you have 'requests' installed: pip install requests
    root = tk.Tk()
    app = TodoGUI(root)
    root.mainloop()