// ================================================
// src/services/todoService.js (Updated)
// This service layer now encapsulates all business logic, ensuring data integrity across sources.
// ================================================

const { Op } = require('sequelize'); 

/** Helper function to fetch local todo by unique identifier or external source ID. */
async function getLocalTodo(uniqueId, sourceType) {
    // Placeholder: In a real implementation, this complex query would search by the incoming ID 
    // and source type to find the existing record.
    console.log(`[DB Lookup] Searching for local todo with ID/Source ${uniqueId} from ${sourceType}.`);
    return null; // Assume no match initially
}

/**
 * Core creation logic, enforcing metadata requirements (Phase 1).
 */
async function createTodo({ title, description = '', due_date, user_id, source }) {
    // ... validation checks on input data ...

    const newTodoRecord = await TodoModel.create({ 
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
async function syncTodoFromSource(incomingData) {
    const { todo_id: incomingId, title, description, due_date, source } = incomingData;
    
    // Check for local record first to determine conflict strategy.
    const localTodo = await getLocalTodo(incomingId, source);

    if (!localTodo) {
        console.log("[SYNC] No local record found. Creating new task.");
        return await createTodo({ title, description, due_date, user_id: incomingData.user_id, source }); 
    } else if (incomingData.updated_at > localTodo.updated_at) {
        console.log("[SYNC] Remote change is newer. Overwriting local record.");
        // Simulate the update call to merge/overwrite data
        return await TodoModel.update(localTodo.todo_id, incomingData); 
    } else if (incomingData.updated_at < localTodo.updated_at) {
         console.warn("[SYNC] Local change is newer. Sync skipped: Preventing accidental overwrite.");
         return localTodo; // Return the existing local record, indicating no changes were applied
    } else {
        // Timestamps are equal (edge case or manual sync). Assume merge needed.
        console.log("[SYNC] Timestamps match. Merging fields and updating 'updated_at'.");
        return await TodoModel.update(localTodo.todo_id, incomingData);
    }
}

/** 
 * External API Integration: Calendar Sync (Phase 2).
 */
async function syncFromCalendar(calendarEvent) {
    // Map the calendar event object to our ToDo schema.
    const mappedData = {
        title: calendarEvent.summary || "Calendar Event Task",
        description: `Source: Google Calendar - ${calendarEvent.location}\n\nDescription from invite body...`,
        due_date: calendarEvent.start.dateTime, // Assumes date object format
        source: 'CALENDAR',
        // Must include user ID for context
        user_id: "fake-user-123" 
    };

    return syncTodoFromSource(mappedData);
}

/** 
 * External API Integration: Email Sync (Phase 2).
 */
async function syncFromEmail(emailMessage) {
     // Map the email message object to our ToDo schema.
    const mappedData = {
        title: `Action Item from ${emailMessage.sender}`, // Use sender for better context
        description: `Original Email Subject: "${emailMessage.subject}"\n\nBody Snippet:\n${emailMessage.snippet}\n\n[Link to original email]`,
        due_date: null, 
        source: 'EMAIL',
        user_id: "fake-user-123"
    };

    return syncTodoFromSource(mappedData);
}


/** 
 * Advanced Feature: Find overdue or high-priority tasks (Phase 3).
 */
async function getOverdueTasks() {
    console.log("\n--- Running Overdue Task Check ---");
    // This query would use Sequelize to find records where due_date < NOW() AND status != COMPLETE
    const overdue = await TodoModel.findAll({
        where: {
            status: 'PENDING',
            due_date: { [Op.lt]: new Date() }
        }
    });
    return overdue;
}


module.exports = {
    createTodo,
    syncFromCalendar,
    syncFromEmail,
    getOverdueTasks,
};