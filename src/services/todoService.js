// ================================================
// src/services/todoService.js (Updated)
// This service layer now encapsulates all business logic, ensuring data integrity across sources.
// ================================================

const { Op } = require('sequelize'); 

/** Helper function to generate UUID (Placeholder) */
function generateUUID() {
    return 'fake-uuid-' + Math.random().toString(36).substring(2, 15);
}

/** Helper function to fetch local todo by unique identifier or external source ID. */
async function getLocalTodo(uniqueId, sourceType) {
    // Placeholder: In a real implementation, this complex query would search by the incoming ID 
    // and source type to find the existing record.
    console.log(`[DB Lookup] Searching for local todo with ID/Source ${uniqueId} from ${sourceType}.`);
    return null; // Assume no match initially
}

/**
 * TodoService class handles all business logic, accepting models as dependencies.
 */
class TodoService {
    constructor(TodoModel) {
        // Store the model instance passed during initialization
         if (!TodoModel) {
            console.error("[FATAL] TodoService initialized with an invalid or incomplete TodoModel dependency.");
            // In a real application, you might throw an error here to prevent usage.
        }
        this.TodoModel = TodoModel;
    }

    /**
    * Creates a new ToDo item in the database.
    * @param {object} todoData - The data for the new ToDo item.
    * @param {string} todoData.title - The title of the task.
    * @param {string} [todoData.status] - Optional status (e.g., PENDING).
    * @param {string} user_id - The ID of the user creating the item.
    * @returns {Promise<object>} A promise that resolves to the created ToDo object.
    */
    async createTodo({ title, description = '', due_date, user_id, source }) {
        // ... validation checks on input data ...

        const newTodoRecord = await this.TodoModel.create({ // <-- Use the stored model instance
            todo_id: generateUUID(), // Assume a UUID generation helper exists
            user_id: user_id,
            title: title,
            description: description,
            due_date: due_date,
            status: 'PENDING',
            source: source || 'MANUAL' // Must set an explicit source!
        });
        console.log(`[SUCCESS] ToDo created with source: ${newTodoRecord.source}`);
        return newTodoRecord;
    }

    /**
     * Synchronization function implementing conflict resolution (Phase 0 & 2).
     */
    async syncTodoFromSource(incomingData) {
        const { todo_id: incomingId, title, description, due_date, source } = incomingData;

        // Check for local record first to determine conflict strategy.
        const localTodo = await getLocalTodo(incomingId, source);

        if (!localTodo) {
            console.log("[SYNC] No local record found. Creating new task.");
            return await this.createTodo({ title, description, due_date, user_id: incomingData.user_id, source });
        } else if (incomingData.updated_at > localTodo.updated_at) {
            console.log("[SYNC] Remote change is newer. Overwriting local record.");
            // Simulate the update call to merge/overwrite data
            return await this.TodoModel.update(localTodo.todo_id, incomingData);
        } else if (incomingData.updated_at < localTodo.updated_at) {
             console.warn("[SYNC] Local change is newer. Sync skipped: Preventing accidental overwrite.");
             return localTodo; // Return the existing local record, indicating no changes were applied
        } else {
            // Timestamps are equal (edge case or manual sync). Assume merge needed.
            console.log("[SYNC] Timestamps match. Merging fields and updating 'updated_at'.");
            return await this.TodoModel.update(localTodo.todo_id, incomingData);
        }
    }

    /**
     * External API Integration: Calendar Sync (Phase 2).
     */
    async syncFromCalendar(calendarEvent) {
        // Map the calendar event object to our ToDo schema.
        const mappedData = {
            title: calendarEvent.summary || "Calendar Event Task",
            description: `Source: Google Calendar - ${calendarEvent.location}\n\nDescription from invite body...`,
            due_date: calendarEvent.start.dateTime, // Assumes date object format
            source: 'CALENDAR',
            // Must include user ID for context
            user_id: "fake-user-123"
        };

        return this.syncTodoFromSource(mappedData);
    }

    /**
     * External API Integration: Email Sync (Phase 2).
     */
    async syncFromEmail(emailMessage) {
         // Map the email message object to our ToDo schema.
        const mappedData = {
            title: `Action Item from ${emailMessage.sender}`, // Use sender for better context
            description: `Original Email Subject: "${emailMessage.subject}"\n\nBody Snippet:\n${emailMessage.snippet}\n\n[Link to original email]`,
            due_date: null,
            source: 'EMAIL',
            user_id: "fake-user-123"
        };

        return this.syncTodoFromSource(mappedData);
    }

    /**
     * Advanced Feature: Find overdue or high-priority tasks (Phase 3).
     */
    async getOverdueTasks() {
        console.log("\n--- Running Overdue Task Check ---");
        // This query would use Sequelize to find records where due_date < NOW() AND status != COMPLETE
        const overdue = await this.TodoModel.findAll({ // <-- Use the stored model instance
            where: {
                status: 'PENDING',
                due_date: { [Op.lt]: new Date() }
            }
        });
        return overdue;
    }
}

// Export a function that initializes and returns the service, requiring the model dependency.
module.exports = (TodoModel) => {
    return new TodoService(TodoModel);
};
