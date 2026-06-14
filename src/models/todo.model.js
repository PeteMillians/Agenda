const { DataTypes } = require('sequelize');

/**
 * TodoModel represents the 'todos' table in the database.
 * NOTE: This assumes you are using Sequelize or a similar ORM setup. 
 * You must initialize this model with your actual sequelize instance.
 */
class TodoModel {
    static define(sequelizeInstance) {
        return sequelizeInstance.define('Todo', {
            todo_id: {
                type: DataTypes.UUID,
                defaultValue: DataTypes.UUIDV4,
                primaryKey: true,
            },
            user_id: {
                type: DataTypes.UUID,
                allowNull: false,
                // Foreign key constraint setup should be handled by the ORM association logic
            },
            title: {
                type: DataTypes.STRING(255),
                allowNull: false,
            },
            description: {
                type: DataTypes.TEXT,
                allowNull: true,
            },
            due_date: {
                type: DataTypes.DATE,
                allowNull: true,
            },
            status: {
                type: DataTypes.ENUM('PENDING', 'COMPLETE', 'SNOOZED'),
                defaultValue: 'PENDING',
                allowNull: false,
            },
            source: {
                type: DataTypes.ENUM('MANUAL', 'CALENDAR', 'EMAIL'),
                allowNull: false,
            },
            created_at: {
                type: DataTypes.DATE,
                defaultValue: DataTypes.NOW,
            },
            updated_at: {
                type: DataTypes.DATE,
                defaultValue: DataTypes.NOW,
            }
        });
    }
}

module.exports = TodoModel;