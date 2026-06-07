# Backend Gateway API Layer Design Specification (GATEWAY_DESIGN)

This document defines the architecture for the central Backend Gateway layer. The Gateway acts as a façade, abstracting the complexity of the three microservices—Calendar, Email, and To-Do—and providing a unified, authenticated API surface to the client/frontend. It is the single point of entry for all external requests.

## 1. Core Responsibilities
The Gateway's primary roles are:
1.  **Authentication & Authorization:** Enforce security rules for every incoming request.
2.  **Orchestration:** Manage complex business logic flows that require multiple service calls (e.g., creating a task and simultaneously scheduling it on the calendar).
3.  **Request/Response Transformation:** Standardize all API requests and responses, regardless of which downstream service generated them.

## 2. Authentication Flow Enforcement
*   All endpoints must validate a Bearer token passed in the `Authorization` header.
*   The gateway middleware must intercept this request and:
    1.  Validate the token's signature/expiry using an internal Identity Provider (IdP).
    2.  Extract and pass the authenticated `user_id` to every downstream service call.
    3.  If validation fails, return a standardized HTTP 401 Unauthorized response immediately.

## 3. Key API Endpoints & Orchestration Logic
The Gateway aggregates client-facing endpoints that manage business workflows:

### A. Event Management (`/api/v1/agenda/events`)
*   **Client Action:** User views 'Calendar View'.
*   **Gateway Logic (Read):** Calls the Calendar Service's read API, passes `user_id` for token context. Aggregates events and translates them into a unified display format suitable for the client UI.

### B. Task Creation & Scheduling (`/api/v1/agenda/schedule`)
*   **Client Action:** User manually creates an event or task that needs to be visible on both the To-Do list and Calendar view.
*   **Gateway Logic (Orchestration - Critical Flow):**
    1.  Receive input payload (Title, Description, Time).
    2.  Call **To-Do Service:** Create a local task record (`status: PENDING`, `source: MANUAL`). Get the new `todo_id`.
    3.  Call **Calendar Service:** Insert event via Google API using the provided data. This call must include the newly created `todo_id` in the metadata/description field, enabling two-way linking.
    4.  Upon success from both services, return a 201 status to the client with confirmation and the linked ID.

### C. Email Processing Trigger (`/api/v1/agenda/sync/email`)
*   **Client Action:** User manually triggers a sync of their inbox.
*   **Gateway Logic (Delegation):** Validates user permissions, then calls the Email Service to start the polling process for new emails associated with the user's account ID.

## 4. Error Handling and Fallbacks
The Gateway must prevent service failures from propagating up as confusing errors:
1.  **Service Failure:** If the Calendar Service fails (e.g., due to rate limits), the Gateway should catch the exception, log it, but still allow partial success if possible. For example, if scheduling an event fails, it should inform the client ("Warning: Could not update Google Calendar," but still return 201 for local creation).
2.  **Consistency Checks:** Implement a health check endpoint (`/health`) that pings the authentication services and basic connectivity checks to all downstream microservices (Calendar Service, Email Service) to ensure operational readiness.