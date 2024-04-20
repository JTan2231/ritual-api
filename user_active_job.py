from datetime import datetime, timedelta

from app import User, app, db

limit = datetime.now() - timedelta(days=4)

if __name__ == "__main__":
    with app.app_context():
        users = User.query.filter(User.last_active > limit).all()
        for i in range(len(users)):
            users[i].active = True

        db.session.commit()

    pass
