// ================================================
// src/index.js
// Main Application Entry Point (Initialization & Wiring)
// This file sets up the database connection and ties together models, services, and routes.
// ================================================

const express = require('express');
const sequelize = require('sequelize'); // Assume Sequelize or similar ORM setup
const UserModel = require('./models/user.model');
const TodoModel = require('./models/todo.model');
const todoRoutes = require('./routes/todoRoutes');
const { createTodo, syncTodoFromSource } = require('./services/todoService');


// --- 1. DATABASE INITIALIZATION (Simulated) ---
const sequelizeInstance = new sequelize({
    dialect: 'sqlite', // Use a safe dialect for demonstration
    storage: './data/agenda.sqlite'
});

// --- 2. MODEL DEFINITION & ASSOCIATION ---
const User = UserModel.define(sequelizeInstance); // Initialize the model with the instance
// NOTE: ToDoModel would also be initialized here in reality, but we keep it separate for clarity.

// Establish relationships (Crucial Step!)
User.hasMany(TodoModel, { foreignKey: 'user_id' });
// We should export this relationship and use it when querying the DB layer.

// --- 3. API ROUTE SETUP ---
const app = express();
app.use(express.json()); // Body parser middleware

// Use the router for ToDo endpoints, which now implicitly uses the connected models/services
app.use('/api/v1/todos', todoRoutes);


// --- 4. RUNTIME INITIALIZATION AND TESTING (Wiring up dependencies) ---
async function initializeDatabase() {
    try {
        await sequelizeInstance.authenticate(); // Check connection
        console.log("✅ Database Connection Successful.");

        // Sync models with database schema (Creates tables if they don't exist)
        await sequelizeInstance.sync({ alter: true }); 
        console.log("✅ Models Synced successfully (Tables ensured).");

        // --- DEMO CALLS TO SHOW WORKFLOW ---
        console.log("\n--- Running Initial Workflow Test ---");
        
        const dummyUser = await User.create({ email: "test@example.com", name: "Test User" });
        
        // 1. Test the creation flow (Model -> Service -> Route)
        const newTodoData = { title: "Test Initial Task", description: "Checking service layer connectivity.", due_date: null };
        const initialTask = await createTodo({ ...newTodoData, user_id: dummyUser.user_id });

        console.log(`✅ Test Task Created Successfully (ID: ${initialTask.todo_id})`);

    } catch (error) {
        console.error("\n🛑 FATAL ERROR DURING APPLICATION INITIALIZATION:", error);
    } finally {
        // Start the server or handle graceful shutdown
        app.listen(3000, () => {
            console.log("\n🚀 Agenda Backend running on http://localhost:3000");
        });
    }
}

initializeDatabase();