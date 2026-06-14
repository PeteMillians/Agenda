const express = require('express');
const router = express.Router();
const todoController = require('../controllers/todoController');

// --- Middleware Placeholder ---
// IMPORTANT: You must implement this middleware to validate the Bearer token 
// and attach user information (like user_id) to req.user, as required by TODO_DESIGN.md.
const authenticateToken = (req, res, next) => {
    /**
     * Middleware function to authenticate a user based on a Bearer token.
     * @param {Object} req - The request object.
     * @param {Object} res - The response object.
     * @param {Function} next - The next middleware function in the stack.
     */

    console.log("Authentication middleware placeholder executed.");
    // For testing Phase 1 without full auth setup, we mock a user ID:
    req.user = { id: 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11' }; // Mock UUID
    next();
};

// --- API Endpoints ---

// GET /api/v1/todos - Retrieve all tasks (supports filtering)
router.get('/', 
    authenticateToken, 
    todoController.getTodos
);

// POST /api/v1/todos - Create a new task
router.post('/', 
    authenticateToken, 
    todoController.createTodo
);

// PUT /api/v1/todos/:todo_id - Update an existing task
router.put('/:todo_id', 
    authenticateToken, 
    todoController.updateTodo
);

module.exports = router;