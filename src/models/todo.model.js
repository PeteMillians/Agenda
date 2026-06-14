const { DataTypes } = require('sequelize');

/**
 * TodoModel represents the 'todos' table in the database.
 * NOTE: This file should NOT be used as a wrapper.
 * The service layer (todoService.js) must receive and use the raw Sequelize Model instance directly.
 */
class TodoModel {
    // Removed static define method as it was causing TypeError and is handled in index.js
    static create(TodoModelInstance, todoData) {
        // Now accepts the already defined Sequelize Model object (e.g., 'Todo')
        return TodoModelInstance.create(todoData);
    }
}

const PlaceholderTodoModel = {
    // Use this object only for type hinting or if required by external code,
    // but the actual model dependency must be passed to TodoService constructor.
};
module.exports = PlaceholderTodoModel;
