const { Op } = require('sequelize'); // Assuming Sequelize usage
const TodoModel = require('../models/todo.model.js');

/**
 * @description Handles GET /api/v1/todos - Retrieves a list of tasks.
 */
exports.getTodos = async (req, res) => {
    try {
        // Extract query parameters for filtering and pagination
        const { status, due_before, limit = 25, offset = 0 } = req.query;
        
        const whereClause = {};
        if (status) whereClause.status = status;
        if (due_before) whereClause.due_date = { [Op.lt]: new Date(due_before) };

        // Fetch data, ensuring sorting by due_date is always applied
        const todos = await TodoModel.todo.findAll({
            where: whereClause,
            order: [['due_date', 'ASC']], // Sort by date ascending
            limit: parseInt(limit),
            offset: parseInt(offset)
        });

        res.status(200).json(todos);
    } catch (error) {
        console.error("Error fetching todos:", error);
        res.status(500).json({ message: "Failed to retrieve tasks." });
    }
};

/**
 * @description Handles POST /api/v1/todos - Creates a new task.
 */
exports.createTodo = async (req, res) => {
    try {
        const { title, description, due_date, source } = req.body;
        
        if (!title || !source) {
            return res.status(400).json({ message: "Title and Source are required." });
        }

        // CRITICAL: User ID must come from the authentication middleware (req.user)
        const userId = req.user ? req.user.id : 'default-user-uuid'; 

        const newTodo = await TodoModel.create({
            user_id: userId,
            title: title,
            description: description || null,
            due_date: due_date ? new Date(due_date) : null,
            source: source,
            status: 'PENDING' // Default status enforced by schema/logic
        });

        res.status(201).json(newTodo);
    } catch (error) {
        console.error("Error creating todo:", error);
        // Handle specific database errors here (e.g., unique constraint violation)
        res.status(500).json({ message: "Failed to create task." });
    }
};

/**
 * @description Handles PUT /api/v1/todos/:todo_id - Updates an existing task.
 */
exports.updateTodo = async (req, res) => {
    try {
        const todoId = req.params.todo_id;
        // Only allow updating fields that are provided in the request body
        const updateData = {
            ...req.body,
            updated_at: new Date() // Always update timestamp on modification
        };

        const updatedTodo = await TodoModel.update(updateData, {
            where: { todo_id: todoId }
        });

        if (updatedTodo[0] === 0) {
             return res.status(404).json({ message: "Task not found." });
        }

        res.status(200).json(updatedTodo[0]);

    } catch (error) {
        console.error("Error updating todo:", error);
        res.status(500).json({ message: "Failed to update task." });
    }
};