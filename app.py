from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI

openai_client = OpenAI()

app = Flask(__name__, static_folder="build", static_url_path="/")
app.config["SQLALCHEMY_DATABASE_URI"] = (
    "mysql+pymysql://ritual:ritualpass@localhost/ritual"
)

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
        activity_begin=datetime.now()
        - timedelta(minutes=int(request.json["duration"])),
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


@app.route("/get-summary", methods=["GET"])
def get_summary():
    begin_date = datetime.strptime(request.args.get("beginDate", ""), "%Y-%m-%d")
    end_date = datetime.strptime(request.args.get("endDate", ""), "%Y-%m-%d")

    activities = Activity.query.filter(
        Activity.activity_begin >= begin_date, Activity.activity_end <= end_date
    ).all()

    activities_prompt = "Activities:\n- " + "\n- ".join(
        f"{a.name} -- {a.activity_begin} - {a.activity_end} -- {a.memo}"
        for a in activities
    )

    oai_response = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[
            {
                "role": "system",
                "content": "You are a world class summarizer and accountability partner. In these messages, you will receive a list of activities and their memos formatted `{activity_name} -- {activity_begin} - {activity_end} -- `{activity_memo}`; for each activity, summarize what has been accomplished based on their memos with a penetrating, empathetic, and uplifting insight. Be careful! You _must_ be concise--_never_ belabor a point! Sing your praises, suggest your critique, and move on (if--_and only if!_--you have any of these to share). _Never_ spend more than a phrase on a topic! If you notice a break in the routine of these activities, point them out! Encourage routine, habits, and rituals above all else. Appreciate nuance, but consider things on a long timescale--minute changes do not matter! What matters is the patterns and changes over the course of *many* activities. Discouragement and critique should _only_ come from unsteady/shaky habits. Praise should come in short supply! _Do not_ overly praise everything the user has done--only that which is deserving! Exercise taste. If you don't have context on why something might be meaningful, _do not_ comment on it.",
            },
            {
                "role": "user",
                "content": activities_prompt,
            },
        ],
    )

    return jsonify({"response": oai_response.choices[0].message.content})


@app.route("/")
def serve():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(use_reloader=True, port=5000)
