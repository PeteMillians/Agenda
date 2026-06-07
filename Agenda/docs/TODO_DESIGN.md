# To-Do List Module Design Specification (TODO_DESIGN)

This document details the technical architecture and implementation plan for the Agenda's internal To-Do list module, focusing on data integrity, API standards, and a resilient local-first synchronization model.

## 1. Database Schema
The `todos` table will be the central persistence unit:

| Field | Type | Description | Constraints | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **todo\_id** | UUID | Unique identifier for the task. | Primary Key, Not Null | |
| **user\_id** | UUID | Foreign key to the user account owner. | Indexed, Not Null | Ensures data segregation. |
| **title** | VARCHAR(255) | The main descriptive title of the task. | Not Null, Max 255 chars | |
| **description** | TEXT | Detailed notes associated with the task. | Optional | Supports rich text (Markdown). |
| **due\_date** | TIMESTAMP | The required completion date and time. | Nullable | Used for sorting/reminders. |
| **status** | ENUM | Task state: `PENDING`, `COMPLETE`, `SNOOZED`. | Default: PENDING, Not Null | Drives UI state. |
| **source** | ENUM | Tracks task origin: `MANUAL`, `CALENDAR`, `EMAIL`. | Not Null | Helps with conflict resolution context. |
| **created\_at** | TIMESTAMP | Timestamp of initial creation. | Auto-generated | For tracking history. |
| **updated\_at** | TIMESTAMP | Timestamp of the last modification to this record. | Auto-generated | Critical for synchronization checks. |

## 2. API Specifications (REST Endpoints)

All endpoints should be prefixed with `/api/v1/todos`. Authentication via Bearer token is required for all requests.

### A. Create To-Do
*   **Endpoint:** `POST /api/v1/todos`
*   **Request Body:**
    ```json
    {
      "title": "Meeting follow up",
      "description": "Send notes to John.",
      "due_date": "2026-07-15T10:00:00Z",
      "source": "MANUAL" 
    }
    ```
*   **Response (201 Created):** Returns the newly created To-Do object, including its generated `todo_id`.

### B. Retrieve To-Dos (List)
*   **Endpoint:** `GET /api/v1/todos`
*   **Query Parameters:**
    *   `status`: Filter by status (`PENDING`, `COMPLETE`).
    *   `due_before`: Only show tasks due before this date.
    *   `limit`: Pagination limit (default: 25).
    *   `offset`: Pagination offset (for large datasets).
*   **Response (200 OK):** Array of To-Do objects, sorted by `due_date`.

### C. Update To-Do
*   **Endpoint:** `PUT /api/v1/todos/{todo_id}`
*   **Request Body (Partial update allowed):**
    ```json
    {
      "status": "COMPLETE",
      "description": "Finalized notes."
    }
    ```
*   **Response (200 OK):** The updated To-Do object.

### D. Delete To-Do
*   **Endpoint:** `DELETE /api/v1/todos/{todo_id}`
*   **Request Body:** None required.
*   **Response (204 No Content):** Success confirmation.

## 3. Local Persistence and Synchronization Strategy

The application utilizes a **Local-First Architecture**. The local database is considered the source of truth for the UI until synchronization occurs.

### A. Data Flow & Conflict Resolution
1.  **Local Write:** When the user performs any action (CRUD), the change is written immediately to the local database, and the `updated_at` timestamp is set to the current server time. This provides instant feedback.
2.  **Background Sync (Push/Pull):** A background worker periodically runs synchronization tasks.
3.  **Conflict Detection:** When pulling changes from a remote source or another device:
    *   The system compares the incoming record's `updated_at` timestamp with the local record's `updated_at`.
    *   If `Remote.updated_at > Local.updated_at`, the remote change is accepted, and the local record is overwritten/merged.
    *   **Conflict:** If both timestamps are recent and different (indicating simultaneous edits), the system will adopt a **Last Write Wins** strategy based on the highest timestamp found, but will also flag the item in the UI and notify the user to manually review potential data loss or conflict areas.

### B. Synchronization Triggers
*   **Initial Load:** On app startup, synchronize all required data from cloud sources (if applicable).
*   **Manual Sync Trigger:** User-initiated sync button click.
*   **Time-Based Polling:** Periodic background checks for new remote changes (e.g., every 15 minutes).