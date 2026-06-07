# Agenda

A dynamic and interactive agenda that integrates with Google Calendar, Gmail, and a built-in to-do list.

## Features

- **Google Calendar Integration** – View, create, and manage events directly from the agenda.
- **Gmail Integration** – Access emails, schedule reminders, and link correspondence to calendar events.
- **To-Do List** – Create, organize, and track tasks alongside your calendar and email.
- **Interactive Interface** – Seamlessly navigate between views and update items in real time.

## Getting Started

1. Clone the repository.
2. Install dependencies (see below).
3. Set up Google API credentials (instructions pending).
4. Run the application.

### Prerequisites

- Node.js
- Google Cloud Platform project with Calendar and Gmail APIs enabled

### Installation

```bash
npm install
```

### Configuration

Create a `.env` file in the project root with your Google OAuth credentials:

```
GOOGLE_CLIENT_ID=your_client_id
GOOGLE_CLIENT_SECRET=your_client_secret
GOOGLE_REDIRECT_URI=http://localhost:3000/auth/callback
```

### Running

```bash
npm start
```

## Project Structure

```
Agenda/
├── src/              # Application source code
├── public/           # Static assets
├── config/           # Configuration files
└── README.md         # This file
```

## Roadmap

- [ ] Google Calendar read/write integration
- [ ] Gmail inbox and search integration
- [ ] To-do list with due dates and priorities
- [ ] Drag-and-drop scheduling
- [ ] Notification and reminder system

## License

MIT
