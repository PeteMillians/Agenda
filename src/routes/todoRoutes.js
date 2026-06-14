const express = require('express');
const router = express.Router();
const todoService = require('../services/todoService');

// Middleware for authentication and user context extraction should be applied here.

/** 
 * POST /api/v1/todos 
 * Endpoint to create a new ToDo item (typically from user input).
 */
router.post('/', async (req, res) => {
    try {
        // In a real app, the user_id would come from an authenticated middleware token.
        const userId = "fake-user-123"; 
        const todoData = req.body;

        if (!todoData || !todoData.title) {
            return res.status(400).json({ error: "Missing required title for the ToDo item." });
        }

        // Use the service layer to handle all business logic and database interactions
        const newTodo = await todoService.createTodo({ 
            ...todoData, 
            user_id: userId 
        });

        res.status(201).json(newTodo);

    } catch (error) {
        console.error("Error creating ToDo item:", error);
        res.status(500).json({ message: "Failed to create To-Do item.", details: error.message });
    }
});

/** 
 * GET /api/v1/todos 
 * Endpoint to fetch the list of tasks (supports query parameters for filtering).
 */
router.get('/', async (req, res) => {
    // Extract filters from request query params (e.g., ?status=PENDING&due_before=2026-07-30)
    const statusFilter = req.query.status; 

    try {
        // In a real implementation, we would call todoService.getList(statusFilter);
        console.log(`[Route Handler] Retrieving todos for filter: ${statusFilter || 'all'}`);
        res.status(200).json({ message: `Successfully fetched list of tasks filtered by ${statusFilter || 'all'} (API Placeholder)` });

    } catch (error) {
        console.error("Error fetching ToDo items:", error);
        res.status(500).json({ message: "Failed to retrieve To-Do list." });
    }
});

module.exports = router;