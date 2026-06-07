# Gmail Integration Service Design Specification (EMAIL_DESIGN)

This document details how Agenda will integrate with Gmail to process emails, extract actionable information, and link reminders/tasks into the local agenda state.

## 1. Polling and Synchronization Mechanism
The goal is to achieve near real-time synchronization without violating API quotas. We must prioritize efficient polling over aggressive webhook usage (which requires more complex infrastructure).

### A. Primary Method: Search and Watch (Hybrid Approach)
1.  **Labeling/Filtering:** The most reliable method is for the user or a dedicated internal process to apply a unique, unread label (e.g., `agenda_unread`) to emails that need processing. This limits search scope significantly.
2.  **Initial Sync:** Use the Gmail API's `users.messages.list` endpoint with specific query parameters and pagination to fetch all messages under the designated label since the last successful sync time (`last_processed_timestamp`).
3.  **Continuous Polling:** Periodically poll this specialized search view (e.g., every 15 minutes). The API call will filter based on `after:LAST_SYNC` date/time marker to ensure we only retrieve new emails.

### B. Error Handling and Resilience
*   **Rate Limiting:** Implement an **Exponential Backoff Strategy**. If the API returns a quota error, the polling worker must pause for $2^n$ seconds (where $n$ is the retry count) before attempting reconnection/retry.
*   **Connection Drops:** The service will wrap all external calls in robust `try...except` blocks to handle network failures gracefully, logging the failure and retrying during the next scheduled cycle.

## 2. Actionable Message Parsing and Filtering
The core intelligence of this service lies in identifying which email contents translate into an Agenda event or task.

### A. Keyword/Regex Engine:
1.  **Task Identification:** Use a configurable set of regular expressions (e.g., `/(due|follow up):?\s*([a-zA-Z0-9]+)\s*(by)?\s*(\d{4}-\d{2}-\d{2})/i`) to extract keywords, due dates, and names from the email body/subject line.
2.  **Event Identification:** Look for phrases like "Meeting scheduled for," or specific calendar inviter formats.
3.  **Confidence Scoring:** Implement a simple scoring system (e.g., High confidence if *both* a date AND a subject keyword are present; Medium confidence if only the date is found).

### B. Contextual Enrichment:
If an email is parsed as a task/event, the service must attempt to fill missing data:
*   **Title:** Use the email's Subject line as the primary title candidate.
*   **Description:** Use the body content, but prioritize extracting clean text blocks over raw HTML parsing.

## 3. Linking Mechanism (The To-Do Bridge)
When an actionable email is identified and a corresponding To-Do/Reminder item is created:

1.  A new record is created in the `todos` table (`source`: `EMAIL`).
2.  This record must store the **Message ID** and the **Snippet Content** of the original email that triggered it. This provides context for the user when reviewing their tasks, allowing them to click through from the To-Do item directly to the source email in Gmail.

## 4. Data Flow Summary
Gmail API $\to$ Polling Worker $\to$ Message Parser (Regex/NLP) $\to$ Validation $\to$ `todos` table (`source: EMAIL`, links message ID).