# System Design Specification for Agenda Application

This document details the technical specifications and implementation plan for the Agenda application, outlining system architecture, service integrations (Google Calendar, Gmail), and core module designs (To-Do List).

## 1. Overall System Architecture

The Agenda application follows a microservice-oriented architecture designed for modularity, scalability, and resilience. The core components interact primarily through message queues or defined API endpoints.

### Component Interactions:

*   **Client/Frontend:** The single point of interaction for the user, handling UI state and making requests to the Backend Gateway.
*   **Backend Gateway (API Layer):** Acts as a routing layer, authenticating requests, managing rate limits, and dispatching tasks to specialized services.
*   **Calendar Service:** Dedicated service responsible solely for interacting with external calendar APIs (Google Calendar). It handles OAuth token refresh and implements the read/write logic for events.
    *   *Interaction:* Receives event creation/retrieval requests from the Gateway; communicates data changes back via internal API calls or database updates.
*   **Email Service:** Dedicated service managing communication with Gmail APIs. This service is responsible for polling, filtering, and linking emails to reminders or tasks.
    *   *Interaction:* Polls Gmail using credentials provided by the Gateway; pushes structured reminder/task data into the Local State/DB.
*   **Local State/Database (Persistence Layer):** A centralized data store (e.g., PostgreSQL) holding all application-specific state, including user profiles, To-Do items, reminders linked to emails, and cached calendar data.

## 2. Google Calendar Integration

### OAuth Flow:
1.  **Authorization Request:** The client redirects the user to the Google OAuth authorization endpoint with appropriate scopes and a `redirect_uri`.
2.  **Consent & Callback:** Upon consent, Google redirects the user back to the defined `redirect_uri` containing an authorization code.
3.  **Token Exchange:** The Backend Gateway intercepts the code and immediately exchanges it for Refresh Token and Access Token using the client credentials.
4.  **Token Management:** The Refresh Token is stored securely in the Local State/DB, allowing the system to automatically generate new Access Tokens before they expire without user intervention.

### Required Scope:
*   `https://www.googleapis.com/auth/calendar` (or more granular scopes if necessary).

### Functionality:

**A. Reading Events:**
*   The Calendar Service will use the `events.list` API endpoint.
*   Retrieval must be scoped by time range (`timeMin`, `timeMax`) and optionally filtered by attendees or specific calendars.
*   Implementation should include caching of fetched events to minimize redundant API calls within a given window (e.g., caching daily views).

**B. Writing Events:**
*   The Calendar Service will use the `events.insert` API endpoint.
*   Events must be enriched with necessary details (Title, Description, Start/End Time, Attendees).
*   When an event is created or updated via Agenda, the service must ensure it records a unique identifier linking it back to the local record in the Local State/DB for synchronization purposes.

## 3. Gmail Integration

### Email Retrieval Strategy:
The Email Service will poll the user's mailbox using the `messages:list` and `users.messages.get` APIs.

1.  **Search Logic:** Initial retrieval will focus on specific labels (e.g., 'reminders', or custom labels applied by Agenda) or use advanced Gmail search syntax (`from:specific@domain with label:agenda`).
2.  **Filtering:** Incoming emails must pass through a content parser that uses NLP/regex to determine if the email contains actionable information suitable for linking to a reminder, task, or calendar event (e.g., containing "Meeting on [Date]" or "Please follow up by [Date]").
3.  **Rate Limiting:** A dedicated rate limiter module must be implemented within the Email Service. We will adhere strictly to Google's published quota limits. The service will implement exponential backoff and retry logic upon receiving `rateLimitExceeded` errors.

### Linking Reminders:
When actionable email content is identified, the Email Service will:
1.Create a record in the Local State/DB linking the specific message ID, relevant snippet, and associated reminder type (e.g., *Reminder*, *Task*).
2.This link ensures that even if the original email is deleted or moved, Agenda retains context about why it was saved/reminded upon.

## 4. To-Do List Module

### Data Schema:

| Field | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `todo_id` | UUID | Unique identifier for the task. | Primary Key |
| `user_id` | UUID | Foreign key to the user account. | Indexed |
| `title` | String | The main descriptive title of the task. | Not Null, Max Length |
| `description` | Text | Detailed notes associated with the task. | Optional |
| `due_date` | DateTime | The required completion date/time. | Nullable |
| `status` | Enum | (Pending, Complete, Snoozed) | Default: Pending |
| `source` | Enum | (Manual, Calendar, Email) | Tracks how the task was created. |
| `created_at` | DateTime | Timestamp of creation. | Auto-generated |

### Persistence Strategy:
The To-Do list will be persisted entirely in the Local State/Database layer, utilizing standard SQL transactions to ensure ACID compliance when updating or creating tasks. No external services are required for persistence.

### Synchronization Plan:
1.  **Local First:** The application operates on a "local-first" principle; all reads and writes are initially against the local database cache.
2.  **Eventual Consistency:** When synchronization with cloud sources (e.g., Google Calendar, or a potential future multi-device setup) is required, the system will implement change detection mechanisms.
    *   The database must track the `last_synced_timestamp` for each major data type (Tasks, Events).
    *   Synchronization routines will poll external APIs only for records modified after this timestamp, minimizing bandwidth and processing overhead.
3.  **Conflict Resolution:** If a task is manually edited locally but an updated version arrives from a source during sync, the system will adopt a "Last Write Wins" strategy based on the highest timestamp found between the local record (`updated_at`) and the incoming data's modification time. Users must be notified of potential conflicts upon manual intervention if ambiguity arises.