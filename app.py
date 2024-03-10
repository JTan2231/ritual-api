from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__, static_folder="build", static_url_path="/")
app.config["SQLALCHEMY_DATABASE_URI"] = "mysql+pymysql://ritual:ritualpass@localhost/ritual"

CORS(app)

db = SQLAlchemy(app)

# TODO: Change this when auth is implemented
TEST_ID = 1


class User(db.Model):
    user_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(256), nullable=False)
    password = db.Column(db.String(256), nullable=False)


class Activity(db.Model):
    activity_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    activity_begin = db.Column(db.DateTime, nullable=False)
    activity_end = db.Column(db.DateTime, nullable=False)
    memo = db.Column(db.String(512), nullable=False)

    user = db.relationship("User", backref=db.backref("activities", lazy=True))


@app.route("/get-activities", methods=["GET"])
def get_activities():
    return jsonify({"message": "todo"})


@app.route("/add-activity", methods=["POST"])
def add_activity():
    new_activity = Activity(
        user_id=1,
        name=request.json["activity_name"],
        activity_begin=datetime.now() - timedelta(minutes=int(request.json["duration"])),
        activity_end=datetime.now(),
        memo=request.json["memo"],
    )

    db.session.add(new_activity)
    response = {}
    try:
        db.session.commit()
        response["message"] = "Activity added successfully"
    except Exception as e:
        print(e)
        response["message"] = "There was an error adding the activity: " + str(e)

    return jsonify(response)


@app.route("/get-summary")
@app.route("/")
def serve():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(use_reloader=True, port=5000)
