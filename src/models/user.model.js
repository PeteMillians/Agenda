const { DataTypes } = require('sequelize');

/**
 * UserModel represents the 'users' table in the database.
 * This model is necessary to associate ToDos with a specific user account.
 */
class UserModel {
    static define(sequelizeInstance) {
        return sequelizeInstance.define('User', {
            user_id: {
                type: DataTypes.UUID,
                defaultValue: DataTypes.UUIDV4,
                primaryKey: true,
            },
            email: {
                type: DataTypes.STRING(255),
                allowNull: false,
                unique: true,
                validate: {
                    isEmail: true
                }
            },
            name: {
                type: DataTypes.STRING(255),
                allowNull: false
            }
        });
    }
}

module.exports = UserModel;