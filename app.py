import json
import os
from base64 import b64decode
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI

openai_client = OpenAI()

app = Flask(__name__, static_folder="build", static_url_path="/")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["RITUAL_DB_URL"]

CORS(app)

db = SQLAlchemy(app)

# TODO: Change this when auth is implemented
TEST_ID = 1
TEMPERATURE = 0.05
DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%H:%M:%S"
DATETIME_FORMAT = f"{DATE_FORMAT} {TIME_FORMAT}"


def authenticate(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization")
        if auth is None:
            return "Unauthorized", 401
        else:
            type, encoded_credentials = auth.split(" ", 1)
            if type.lower() == "basic":
                username, password = (
                    b64decode(encoded_credentials).decode("utf-8").split(":", 1)
                )
            else:
                return "Unauthorized", 401

            user = User.query.filter_by(username=username).one()

            if password == user.password:
                request.user_id = user.user_id
                return func(*args, **kwargs)
            else:
                return "Unauthorized", 401

    return wrapper


class User(db.Model):
    user_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(256), nullable=False, unique=True)
    password = db.Column(db.String(256), nullable=False)


class Activity(db.Model):
    activity_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    activity_begin = db.Column(db.DateTime, nullable=False)
    activity_end = db.Column(db.DateTime, nullable=False)
    memo = db.Column(db.String(512), nullable=False)

    user = db.relationship("User", backref=db.backref("activities", lazy=True))


def activities_to_json(activities):
    jsons = []
    for a in activities:
        jsons.append(
            {
                "activity_name": a.name,
                "activity_begin": a.activity_begin.strftime(DATETIME_FORMAT),
                "activity_end": a.activity_end.strftime(DATETIME_FORMAT),
                "memo": a.memo,
            }
        )

    return jsons


def date_from_datetime(datetime_string):
    return datetime.strftime(
        datetime.strptime(datetime_string, DATETIME_FORMAT), DATE_FORMAT
    )


def time_from_datetime(datetime_string):
    return datetime.strftime(
        datetime.strptime(datetime_string, DATETIME_FORMAT), TIME_FORMAT
    )


# makes datetime with current date
def time_to_datetime(time_string):
    return datetime.combine(
        date.today(), datetime.strptime(time_string, TIME_FORMAT).time()
    ).strftime(DATETIME_FORMAT)


def group_by_days(activities):
    days = {}
    for a in activities:
        begin = date_from_datetime(a["activity_begin"])

        if begin in days:
            days[begin].append(a)
        else:
            days[begin] = [a]

    days = {
        key: sorted(value, key=lambda x: x["activity_begin"])
        for key, value in days.items()
    }

    return days


# TODO: better error handling when this fails
def activity_from_chat(chat):
    oai_response = openai_client.chat.completions.create(
        model="gpt-4",
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "system",
                "content": f'You are an assistant designed to convert conversationally-styled messages describing the user\'s activities and turn them into a list of objects, each object of the following JSON format: {{ "activity_name": "name of activity (string)", "activity_begin": "timestamp ({TIME_FORMAT}) of when the activity began", "activity_end": "timestamp ({TIME_FORMAT}) of when the activity ended", "memo": "a string describing the details of the activity" }}',
            },
            {"role": "user", "content": chat},
        ],
    )

    try:
        activities = json.loads(oai_response.choices[0].message.content)
        for a in activities:
            a["activity_begin"] = time_to_datetime(a["activity_begin"])
            a["activity_end"] = time_to_datetime(a["activity_end"])

        return activities
    except Exception as e:
        print(oai_response)
        print("error parsing OAI response: " + str(e))
        return {}


def format_activity_json_to_display(activities):
    grouped = group_by_days(activities)
    for g in grouped.values():
        for a in g:
            a["activity_begin"] = time_from_datetime(a["activity_begin"])
            a["activity_end"] = time_from_datetime(a["activity_end"])

    return grouped


# temporary route; merge this with `add-activity`
@app.route("/chat", methods=["POST"])
@authenticate
def chat():
    chat_json = activity_from_chat(request.json["chat"])

    return jsonify(format_activity_json_to_display(chat_json))


@app.route("/create-account", methods=["POST"])
def create_account():
    new_user = User(
        username=request.json["username"], password=request.json["password"]
    )

    db.session.add(new_user)
    response = {}
    try:
        db.session.commit()
        response["message"] = "User successfully created"
    except Exception as e:
        print(e)
        response["message"] = "There was an error creating the user: " + str(e)

    return jsonify(response)


@app.route("/get-activities", methods=["GET"])
@authenticate
def get_activities():
    begin_date = datetime.strptime(request.args.get("beginDate", ""), DATE_FORMAT)
    end_date = datetime.strptime(request.args.get("endDate", ""), DATE_FORMAT)

    activities = Activity.query.filter(
        Activity.activity_begin >= begin_date,
        Activity.activity_end <= end_date,
        Activity.user_id == request.user_id,
    ).all()

    activities = activities_to_json(activities)

    return jsonify(format_activity_json_to_display(activities))


@app.route("/add-activities", methods=["POST"])
@authenticate
def add_activities():
    for a in request.json:
        db.session.add(
            Activity(
                user_id=request.user_id,
                name=a["activity_name"],
                activity_begin=time_to_datetime(a["activity_begin"]),
                activity_end=time_to_datetime(a["activity_end"]),
                memo=a["memo"],
            )
        )

    response = {}
    try:
        db.session.commit()
        response["message"] = "Activities added successfully"
    except Exception as e:
        print(e)
        response["message"] = "There was an error adding the activities: " + str(e)

    return jsonify(response)


@app.route("/add-activity", methods=["POST"])
@authenticate
def add_activity():
    new_activity = Activity(
        user_id=request.user_id,
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
@authenticate
def get_summary():
    begin_date = datetime.strptime(request.args.get("beginDate", ""), DATE_FORMAT)
    end_date = datetime.strptime(request.args.get("endDate", ""), DATE_FORMAT)

    activities = Activity.query.filter(
        Activity.activity_begin >= begin_date,
        Activity.activity_end <= end_date,
        Activity.user_id == request.user_id,
    ).all()

    activities_prompt = "Activities:\n- " + "\n- ".join(
        f"{a.name} -- {a.activity_begin} - {a.activity_end} -- {a.memo}"
        for a in activities
    )

    oai_response = openai_client.chat.completions.create(
        model="gpt-4",
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "system",
                "content": "You are a world class accountability partner. In these messages, you will receive a list of activities and their memos formatted `{activity_name} -- {activity_begin} - {activity_end} -- `{activity_memo}`; summarize what has been accomplished based on their memos with a penetrating and empathetic insight. Be careful! You _must_ be concise--_never_ belabor a point! Sing your praises, suggest your critique, and move on (if--_and only if!_--you have any of these to share). _Never_ spend more than a phrase on a topic! If you notice a break in the routine of these activities, point them out! Encourage routine, habits, and rituals above all else. Appreciate nuance, but consider things on a long timescale--minute changes do not matter! What matters is the patterns and changes over the course of *many* activities. Discouragement and critique should _only_ come from unsteady/shaky habits. Praise should come in short supply! _Do not_ overly praise everything the user has done--only that which is deserving! Exercise taste. If you don't have context on why something might be meaningful, _do not_ comment on it.",
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
