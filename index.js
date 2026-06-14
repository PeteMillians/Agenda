// ================================================
// src/index.js
// Main Application Entry Point (Initialization & Wiring)
// This file sets up the database connection and ties together models, services, and routes.
// ================================================


const express = require('express');
const sequelize = require('sequelize'); // Assume Sequelize or similar ORM setup
const UserModel = require('./src/models/user.model');
const TodoModel = require('./src/models/todo.model');
const todoRoutes = require('./src/routes/todoRoutes');
const { createTodo, syncTodoFromSource } = require('./src/services/todoService');


// --- 1. DATABASE INITIALIZATION (Simulated) ---
const sequelizeInstance = new sequelize({
    dialect: 'sqlite', // Use a safe dialect for demonstration
    storage: './data/agenda.sqlite'
});

// --- 2. MODEL DEFINITION & ASSOCIATION ---



// FIX: Define models directly on the sequelize instance, which is the correct Sequelize pattern.
const User = sequelizeInstance.define('User', UserModel);
const Todo = sequelizeInstance.define('Todo', TodoModel); // Use the attributes/definition from TodoModel
// Establish relationships (Crucial Step!)
User.hasMany(Todo, { foreignKey: 'user_id' });
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

        // FIX: Pass the defined 'Todo' model object to createTodo, and use TodoModel.create with it.
        const initialTask = await createTodo(Todo, { ...newTodoData, user_id: dummyUser.user_id });

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