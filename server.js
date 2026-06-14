const express = require('express');
const todoRoutes = require('./src/routes/todoRoutes');

// Initialize the Express application
const app = express();
const PORT = process.env.PORT || 3000;

// Middleware to parse JSON bodies
app.use(express.json());

// --- API Routes ---
// Mount the To-Do routes under /api/v1/todos
app.use('/api/v1/todos', todoRoutes);

// Basic root route for health check or welcome message
app.get('/', (req, res) => {
    res.status(200).json({ 
        message: "Agenda Backend API is running successfully.",
        version: "1.0"
    });
});


// --- Server Listener ---
const server = app.listen(PORT, () => {
    console.log(`✅ Agenda Backend Server listening on port ${PORT}`);
    console.log('----------------------------------------------------');
    console.log('To test the API endpoints, use a tool like Postman or curl.');
});

// Handle graceful shutdown (optional but good practice)
process.on('SIGINT', () => {
    console.log('\n🛑 Received SIGINT. Shutting down server...');
    server.close(() => {
        console.log('Server closed gracefully.');
        process.exit(0);
    });
});