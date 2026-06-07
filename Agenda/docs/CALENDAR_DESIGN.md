# Google Calendar Service Design Specification (CALENDAR_DESIGN)

This document details the technical implementation plan for integrating Agenda with Google Calendar, ensuring secure and reliable event management.

## 1. OAuth 2.0 Flow Implementation
The system must use a standard three-legged OAuth 2.0 flow to grant access while minimizing security risks.

### A. Workflow Steps:
1.  **Initiation:** The user clicks "Connect Google Calendar," which redirects the browser to the Google authorization endpoint.
2.  **Scope Definition:** The request will explicitly use the scope: `https://www.googleapis.com/auth/calendar`. This scope grants necessary read and write permissions for events, but should be limited strictly to what is needed.
3.  **Callback & Code Exchange:** Upon user consent, Google redirects back to our defined `redirect_uri` with a temporary authorization code. The Backend Gateway must immediately exchange this code for the **Access Token** and the critical **Refresh Token**.
4.  **Secure Storage (Token Management):**
    *   The **Refresh Token** is the most sensitive asset, as it allows indefinite access without user intervention. It must be encrypted at rest in the Local State/DB using a dedicated encryption key managed by the Secrets Manager.
    *   The Access Token lifespan is short (minutes/hours). The service must implement an automatic background check: before making any API call, it checks the token expiration time and automatically uses the Refresh Token to acquire a new Access Token if necessary, without user knowledge.

## 2. Calendar Service APIs
The dedicated Calendar Service layer will abstract all Google API calls.

### A. Reading Events (GET /events)
*   **Purpose:** Retrieves events for a given user/time range.
*   **Endpoint Details:** The service will use the `events.list` method.
*   **Required Parameters:**
    *   `calendarId`: Specifies which calendar to check (e.g., primary, work).
    *   `start`: Start timestamp of the query range (`timeMin`).
    *   `end`: End timestamp of the query range (`timeMax`).
    *   `singleEvents`: Must be set to `true` to ensure all returned items are full event objects.
*   **Optimization & Caching:** To reduce API quota usage, a local cache layer will store events for the current view (e.g., cached views for the last 30 days) and only request data that has changed or falls outside the cache window.

### B. Writing Events (POST /events)
*   **Purpose:** Creates or updates an event in Google Calendar.
*   **Methodology:** The service must map the internal Agenda To-Do/Event object structure to the required `Event` resource body for the Google API.
*   **Cross-Referencing:** When creating a calendar event from a local Agenda task:
    1.  The service will create the event via `events.insert`.
    2.  Crucially, it must immediately record the unique **Google Event ID** returned in the response into the Local State/DB record associated with that task. This ID is necessary for future updates or deletions.

## 3. Error Handling & Resilience
*   **Rate Limiting:** The service must implement an exponential backoff and retry mechanism (e.g., wait $2^n$ seconds) when Google API returns quota exceeded errors, rather than failing immediately.
*   **Authorization Failure:** If token refresh fails repeatedly (suggesting the user revoked access or tokens expired), the service must gracefully fail, alerting the user via the UI to re-authenticate.