from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

def init_db(app):
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///expenses.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _migrate_user_table(app)


def _migrate_user_table(app):
    """
    Safely adds any missing columns to the user table.
    This lets the app upgrade an existing database without losing data,
    instead of crashing when new columns are added to the model.
    """
    try:
        inspector = inspect(db.engine)
        if 'user' not in inspector.get_table_names():
            return

        existing_columns = [col['name'] for col in inspector.get_columns('user')]

        with db.engine.connect() as conn:
            if 'last_login' not in existing_columns:
                conn.execute(text('ALTER TABLE user ADD COLUMN last_login DATETIME'))
                conn.commit()
            if 'login_count' not in existing_columns:
                conn.execute(text('ALTER TABLE user ADD COLUMN login_count INTEGER DEFAULT 0'))
                conn.commit()
    except Exception as e:
        print('Migration check skipped:', e)
