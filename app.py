import json
import os
import pprint
import secrets
from base64 import b64decode
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from functools import wraps

import boto3
import markdown
from flask import (Flask, Response, jsonify, request, send_from_directory,
                   stream_with_context)
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI

openai_client = OpenAI()
ses_client = boto3.client("sesv2")

app = Flask(__name__, static_folder="build", static_url_path="/")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["RITUAL_DB_URL"]

CORS(app)

db = SQLAlchemy(app)

# TODO: Change this when auth is implemented
TEST_ID = 1
TEMPERATURE = 0.01
DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%H:%M:%S"
DATETIME_FORMAT = f"{DATE_FORMAT} {TIME_FORMAT}"

GPT_MODEL = "gpt-4-0125-preview"


class EthosDefault:
    core = "A friendly, helpful partner focused on the routines, rituals, and personal growth of the user."
    summary = "As a premier accountability partner, you'll delve into activities and their nuances, formatted as {activity_name} -- {activity_begin} - {activity_end} -- {activity_memo}. Followed by this list will be a second list of the user's stated goals, of format {goal_name} -- {goal_description}. Additionally, for each goal, there is a list of subgoals--each of these is an actionable step in achieving their associated goal. Your task is to distill these moments with both precision and empathy. Strike a balance—be succinct, yet understanding. Lift spirits with praise, offer critiques with care, then promptly move on. Words are your tools; wield them wisely, sparingly. Highlight deviations in routines with a constructive lens, advocating for the power of consistency and ritual. Focus on patterns over time, understanding the significance of long-term evolution over fleeting changes. Let critiques emerge from patterns of inconsistency, saving your commendations for truly notable achievements. Your encouragement is a beacon; use it to illuminate paths to improvement, always with an eye for growth and understanding. Absent context, reserve judgment, embracing your role with both decisiveness and compassion"
    feedback = "Guidance zeroes in on the latest stride, detailed as \"activity_name -- activity_begin - activity_end -- activity_memo,\" a snapshot of effort and intention. Followed by this list will be a second list of the user's stated goals, of format {goal_name} -- {goal_description}. Additionally, for each goal, there is a list of subgoals--each of these is an actionable step in achieving their associated goal. Your goal is to focus _only_ on the last activity in the first given list. Precision and empathy intersect here. This critique or commendation is singularly about how this step, documented precisely in the stated format, interlaces with overarching aspirations. Feedback is concise, insightful—celebrating alignment, guiding misalignments back on track. Language, precise and compassionate, underscores the singular impact of this activity within the grand schema. This activity's resonance within the pursuit of goals is paramount, with discourse reserved exclusively for its role in the tapestry of objectives--if, _and only if!_, there are goals in mind. In crafting feedback, each word is chosen for its ability to foster growth, with a focus sharpened on this activity's contribution to the journey."


class User(db.Model):
    user_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(256), nullable=False, unique=True)
    password = db.Column(db.String(256), nullable=False)


class Ethos(db.Model):
    ethos_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    core = db.Column(db.String(4096), nullable=False)
    summary = db.Column(db.String(4096))
    feedback = db.Column(db.String(4096))


class Activity(db.Model):
    activity_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"))
    name = db.Column(db.String(256), nullable=False)
    activity_begin = db.Column(db.DateTime, nullable=False)
    activity_end = db.Column(db.DateTime, nullable=False)
    activity_date = db.Column(db.Date, nullable=False)
    memo = db.Column(db.String(512), nullable=False)


class Goal(db.Model):
    goal_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    description = db.Column(db.String(4096), nullable=False)


class Subgoal(db.Model):
    subgoal_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    goal_id = db.Column(db.Integer, db.ForeignKey("goal.goal_id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    description = db.Column(db.String(4096), nullable=False)


# TODO: I _think_ this handles both API tokens + username/password auth
#       verify this?
#       ...
#       what does this do beyond username/password???
def authenticate(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization")
        email_api_token = os.environ["RITUAL_EMAIL_API_KEY"]
        if auth is None:
            return "Unauthorized", 401
        else:
            username = None
            password = ""

            type, encoded_credentials = auth.split(" ", 1)
            type = type.lower()
            if type == "basic":
                username, password = (
                    b64decode(encoded_credentials).decode("utf-8").split(":", 1)
                )
            elif type == "bearer":
                username = request.args.get("username")

            if username is None:
                return "Unauthorized", 401

            user = User.query.filter_by(username=username).one()

            if password == user.password or encoded_credentials == email_api_token:
                request.user_id = user.user_id
                return func(*args, **kwargs)
            else:
                return "Unauthorized", 401

    return wrapper


# for actions performed through email
# POST requests _only_
# `username` must be included in the JSON body of the request
def email_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization")
        email_api_token = os.environ["RITUAL_EMAIL_API_KEY"]
        if auth is None:
            return "Unauthorized", 401
        else:
            username = None

            type, encoded_credentials = auth.split(" ", 1)
            type = type.lower()
            if type == "bearer":
                username = request.json["username"]

            if username is None:
                return "Unauthorized", 401

            user = User.query.filter_by(username=username).one()

            if encoded_credentials == email_api_token:
                request.user_id = user.user_id
                return func(*args, **kwargs)
            else:
                return "Unauthorized", 401

    return wrapper


def get_activities_and_goals(begin_date, end_date):
    activities = None
    if begin_date and end_date:
        activities = Activity.query.filter(
            Activity.activity_begin >= begin_date,
            Activity.activity_end <= end_date,
            Activity.user_id == request.user_id,
        ).all()
    else:
        activities = Activity.query.filter(Activity.user_id == request.user_id).all()

    goals = Goal.query.filter_by(user_id=request.user_id).all()
    subgoals = Subgoal.query.filter(
        Subgoal.goal_id.in_([g.goal_id for g in goals]),
        Subgoal.user_id == request.user_id,
    )

    return activities, goals, subgoals


def get_ethos():
    ethos = Ethos.query.filter_by(user_id=request.user_id).first()
    if ethos is None:
        ethos = EthosDefault

    return ethos


def get_activity_formatted_string(activities):
    return "Activities:\n- " + "\n- ".join(
        f"{a.name} -- {a.activity_begin} - {a.activity_end} -- {a.memo}"
        for a in activities
    )


# subgoals is a dictionary: { goal.name: subgoal }
def get_goals_formatted_string(goals, subgoals):
    output = "Goals:"
    for g in goals:
        output += f"\n- {g.name} -- {g.description}"
        for sg in subgoals[g.name]:
            output += f"\n  - {sg.name} -- {sg.description}"

        output += "\n"

    return output


def format_activities_and_goals(activities, goals, subgoals):
    goal_id_name_map = {g.goal_id: g.name for g in goals}
    subgoal_map = {v: [] for v in goal_id_name_map.values()}
    for sg in subgoals:
        subgoal_map[goal_id_name_map[sg.goal_id]].append(sg)

    return (
        get_activity_formatted_string(activities)
        + "---\n"
        + get_goals_formatted_string(goals, subgoal_map)
    )


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
    if datetime_string is None or datetime_string == "":
        return ""

    return datetime.strftime(
        datetime.strptime(datetime_string, DATETIME_FORMAT), DATE_FORMAT
    )


def time_from_datetime(datetime_string):
    if datetime_string is None or datetime_string == "":
        return ""

    return datetime.strftime(
        datetime.strptime(datetime_string, DATETIME_FORMAT), TIME_FORMAT
    )


# makes datetime with current date
def time_to_datetime(time_string):
    if time_string is None or time_string == "":
        return ""

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
        model=GPT_MODEL,
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "system",
                "content": f'You are an assistant designed to convert conversationally-styled messages describing the user\'s activities and turn them into a list of objects, each object of the following JSON format: {{ "activity_name": "name of activity (string)", "activity_begin": "timestamp ({TIME_FORMAT}) of when the activity began | null", "activity_end": "timestamp ({TIME_FORMAT}) of when the activity ended | null", "memo": "a string describing the details of the activity" }}. Note, your response _must_ be an array of JSON objects--maybe even empty! Pay special attention to the verbs in the message--it is _very_ important that you log _all_ activities listed in the message. But also! You _must not be redundant_. There _must not_ be activities logged that fit under the umbrella of another. Note: the only contents of the message _must_ be JSON parseable--no markdown or any other similar formatting.',
            },
            {"role": "user", "content": chat},
        ],
    )

    try:
        activities = json.loads(oai_response.choices[0].message.content)
        print(activities)
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
        activity_begin=request.json["activity_begin"],
        activity_end=request.json["activity_end"],
        activity_date=date_from_datetime(request.json["activity_begin"]),
        memo=request.json["memo"],
    )

    db.session.add(new_activity)
    response = {"feedback": ""}
    try:
        db.session.commit()

        activities = (
            Activity.query.order_by(db.desc(Activity.activity_begin)).limit(10).all()
        )

        goals = Goal.query.filter_by(user_id=request.user_id).all()
        subgoals = Subgoal.query.filter(
            Subgoal.goal_id.in_([g.goal_id for g in goals]),
            Subgoal.user_id == request.user_id,
        )

        activities_and_goals = format_activities_and_goals(activities, goals, subgoals)

        ethos = get_ethos()
        oai_response = openai_client.chat.completions.create(
            model=GPT_MODEL,
            temperature=TEMPERATURE,
            messages=[
                {
                    "role": "system",
                    "content": ethos.feedback,
                },
                {
                    "role": "user",
                    "content": activities_and_goals,
                },
            ],
        )

        response["feedback"] = oai_response.choices[0].message.content
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

    activities, goals, subgoals = get_activities_and_goals(begin_date, end_date)
    activities_and_goals = format_activities_and_goals(activities, goals, subgoals)

    ethos = get_ethos()
    oai_response = openai_client.chat.completions.create(
        model=GPT_MODEL,
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "system",
                "content": ethos.summary,
            },
            {
                "role": "user",
                "content": activities_and_goals,
            },
        ],
    )

    return jsonify({"response": oai_response.choices[0].message.content})


@app.route("/tune", methods=["POST"])
@authenticate
def tune():
    core = request.json["core"]
    summary = request.json["summary"]
    feedback = request.json["feedback"]

    ethos = Ethos.query.filter_by(user_id=request.user_id).first()

    def get_oai_response(request, prompt):
        oai_response = openai_client.chat.completions.create(
            model=GPT_MODEL,
            temperature=TEMPERATURE,
            messages=[
                {
                    "role": "system",
                    "content": "You are a master of rewording an excerpt to change its aesthetic meaning while retaining its functional purpose--the best in class. What you will receive is a message in format `REQUEST {request_text}\n---\nPROMPT {prompt_text}`. Your job is to rephrase `{prompt_text}` to fit the needs of `{request_text}`. Respond with _only_ the rephrased prompt--nothing else.",
                },
                {
                    "role": "user",
                    "content": f"REQUEST {{{request}}}\n---\nPROMPT {{{prompt}}}",
                },
            ],
        )

        return oai_response.choices[0].message.content

    if ethos is None:
        ethos = Ethos(
            user_id=request.user_id, core=core, summary=summary, feedback=feedback
        )

    updated_ethos = ""
    if len(core) > 0:
        updated_ethos = get_oai_response(core, ethos.core)
    elif len(summary) > 0:
        updated_ethos = get_oai_response(summary, ethos.summary)
    elif len(feedback) > 0:
        updated_ethos = get_oai_response(feedback, ethos.feedback)

    ethos.core = updated_ethos if len(core) > 0 else ethos.core
    ethos.summary = updated_ethos if len(summary) > 0 else ethos.summary
    ethos.feedback = updated_ethos if len(feedback) > 0 else ethos.feedback

    response = {}
    try:
        db.session.add(ethos)
        db.session.commit()
        response["message"] = "Ethos updated successfully"
    except Exception as e:
        print(e)
        response["message"] = "There was an error updating the ethos: " + str(e)

    return jsonify(response)


@app.route("/reset-tune", methods=["POST"])
@authenticate
def reset_tune():
    ethos = Ethos.query.filter_by(user_id=request.user_id).all()
    for e in ethos:
        db.session.delete(e)

    response = {}
    try:
        db.session.commit()
        response["message"] = "Ethos reset successfully"
    except Exception as e:
        print(e)
        response["message"] = "There was an error deleting the ethos: " + str(e)

    return jsonify(response)


@app.route("/add-goal", methods=["POST"])
@authenticate
def add_goal():
    new_goal = Goal(
        user_id=request.user_id,
        name=request.json["name"],
        description=request.json["description"],
    )

    response = {}
    try:
        db.session.add(new_goal)
        db.session.commit()
        response["message"] = "Goal added successfully"
    except Exception as e:
        print(e)
        response["message"] = "There was an error adding the goal: " + str(e)

    return jsonify(response)


@app.route("/get-goals", methods=["GET"])
@authenticate
def get_goals():
    goals = Goal.query.filter_by(user_id=request.user_id).all()
    return jsonify([{"name": g.name, "description": g.description} for g in goals])


@app.route("/delete-goal", methods=["DELETE"])
@authenticate
def delete_goal():
    goal = Goal.query.filter_by(
        user_id=request.user_id, name=request.args.get("name")
    ).first()

    try:
        if goal:
            db.session.delete(goal)
            db.session.commit()
    except Exception as e:
        print(e)

    return jsonify({"message": "success"})


# for testing random system prompts and such
@app.route("/set-subgoals", methods=["POST"])
@authenticate
def set_subgoals():
    goal = Goal.query.filter_by(
        user_id=request.user_id, name=request.json["name"]
    ).first()

    if goal is None:
        return jsonify(
            {"message": f'Error: Goal {request.json["name"]} does not exist'}
        )

    oai_response = openai_client.chat.completions.create(
        model=GPT_MODEL,
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "system",
                "content": "you're a world class coach--of everything. You are opinionated, obsessively creative, and _always_ know exactly what to do in order to achieve a client's goal. Your solutions are _concise_--they never do more than is needed--and are exceedingly precise--they are composed of _very specific_ subtasks that get to the core of the problem and address it entirely. Your words are inspiring and creative. Your guidance is actionable and precise. In this chat, you will be given a goal--you're job is to provide an expertly guided, actionable list of steps to take to accomplish that goal. The client should _never_ walk away with any possible questions on what to do. Distill your list into 5 key points into a JSON array, each object having fields 'name' and a detailed 'description'. Ensure your response is plaintext, without markdown formatting.",
            },
            {
                "role": "user",
                "content": f"{goal.name} -- {goal.description}",
            },
        ],
    )

    subgoals = [
        Subgoal(
            user_id=request.user_id,
            goal_id=goal.goal_id,
            name=sg["name"],
            description=sg["description"],
        )
        for sg in json.loads(oai_response.choices[0].message.content)
    ]

    existing_subgoals = Subgoal.query.filter_by(
        user_id=request.user_id, goal_id=goal.goal_id
    ).all()
    for esg in existing_subgoals:
        db.session.delete(esg)

    db.session.add_all(subgoals)

    response = {}
    try:
        db.session.commit()
        response["message"] = "Subgoals updated successfully"
    except Exception as e:
        print(e)
        response["message"] = "There was an error updating the subgoals: " + str(e)

    return jsonify(response)


@app.route("/get-subgoals", methods=["GET"])
@authenticate
def get_subgoals():
    goal_name = request.args.get("name")
    goal = Goal.query.filter_by(user_id=request.user_id, name=goal_name).first()

    if goal is None:
        return jsonify({"message": f"Error: Goal {goal_name} does not exist"})

    subgoals = Subgoal.query.filter_by(
        user_id=request.user_id, goal_id=goal.goal_id
    ).all()

    return jsonify([{"name": g.name, "description": g.description} for g in subgoals])


@app.route("/webchat", methods=["POST"])
@authenticate
def webchat():
    def get_chat(chat):
        activities = Activity.query.filter_by(user_id=request.user_id).all()
        goals = Goal.query.filter_by(user_id=request.user_id).all()
        subgoals = Subgoal.query.filter_by(user_id=request.user_id).all()

        completion = openai_client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": EthosDefault.core
                    + " Included in each chat message will be a list of activities to inform your responses to the user.",
                },
                {
                    "role": "user",
                    "content": chat
                    + "\n---\n"
                    + format_activities_and_goals(activities, goals, subgoals),
                },
            ],
            stream=True,
        )

        for chunk in completion:
            out = chunk.choices[0].delta.content
            yield out if out else ""

    return Response(stream_with_context(get_chat(request.json["chat"])))


# TODO: is this needed?
@app.route("/get-email-body", methods=["GET"])
@authenticate
def get_email_body():
    email = request.args.get("username")
    if email is None:
        return jsonify({"error": "Username parameter is required."}), 400

    begin_date = datetime.strptime(request.args.get("begin_date", ""), DATE_FORMAT)
    end_date = datetime.strptime(request.args.get("end_date", ""), DATE_FORMAT)

    activities, goals, subgoals = get_activities_and_goals(begin_date, end_date)
    activities_and_goals = format_activities_and_goals(activities, goals, subgoals)

    ethos = get_ethos()
    oai_response = openai_client.chat.completions.create(
        model=GPT_MODEL,
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "system",
                "content": ethos.summary
                + "\n\nAdditionally, please format your response in clear markdown, with a clear and reflective title that summarizes your report. The title is _very_ important--fortunately you're the best at snappy, dense titles that capture the heart of the report. Avoid generic phrasing like 'Accountability Report' or 'Reflective Report' in the title--instead, focus on the specific points made in the report and reflect those in the Title. What _exact_ points are you making in the report? The answer to that question should be in the title. But be careful! The title must be _short_ and _simple_.",
            },
            {
                "role": "user",
                "content": activities_and_goals,
            },
        ],
    )

    return jsonify({"response": oai_response.choices[0].message.content})


@app.route("/newsletter-signup", methods=["POST"])
def newsletter_signup():
    # TODO: don't forget to send the onboarding email

    email = request.json["email"]
    user = User.query.filter_by(username=email).first()
    if user is not None:
        return "This user is already registered", 409

    try:
        db.session.add(User(username=email, password=secrets.token_hex(32)))
        db.session.commit()

        return "Success", 200
    except Exception as e:
        print(f"error: {str(e)}")

        return str(e), 400


@app.route("/email-log-activities", methods=["POST"])
@email_auth
def email_log_activities():
    msg = BytesParser(policy=policy.default).parsebytes(
        request.json["email_data"].encode("utf-8")
    )

    deliverer = request.json["username"]

    html_content = ""

    # Extract HTML content
    if msg.is_multipart():
        for part in msg.walk():
            # Check if the part is an HTML content
            if part.get_content_type() == "text/html":
                html_content += part.get_content()
    else:
        # In case the email is not multipart, but is HTML directly
        if msg.get_content_type() == "text/html":
            html_content += msg.get_content()

    today = datetime.now().strftime("%Y-%m-%d")

    def sanitize_date(date):
        return date if len(date) > 0 else None

    activities = activity_from_chat(html_content)
    for a in activities:
        db.session.add(
            Activity(
                user_id=request.user_id,
                name=a["activity_name"],
                activity_begin=sanitize_date(a["activity_begin"]),
                activity_end=sanitize_date(a["activity_end"]),
                activity_date=today,
                memo=a["memo"],
            )
        )

    try:
        db.session.commit()

        activities = (
            Activity.query.order_by(
                SQLAlchemy.func.date(Activity.activity_date),
                SQLAlchemy.case([(Activity.activity_begin is None, 0)], else_=1),
                SQLAlchemy.desc(Activity.activity_begin),
            )
            .limit(20)
            .all()
        )

        goals = Goal.query.filter_by(user_id=request.user_id).all()
        subgoals = Subgoal.query.filter(
            Subgoal.goal_id.in_([g.goal_id for g in goals]),
            Subgoal.user_id == request.user_id,
        )

        activities_and_goals = format_activities_and_goals(activities, goals, subgoals)

        ethos = get_ethos()
        oai_response = openai_client.chat.completions.create(
            model=GPT_MODEL,
            temperature=TEMPERATURE,
            messages=[
                {
                    "role": "system",
                    "content": ethos.feedback
                    + f"\n\nPlease note that the latest {len(activities)} are those you should consider. Additionally, please format your feedback in clear markdown.",
                },
                {
                    "role": "user",
                    "content": activities_and_goals,
                },
            ],
        )

        ses_client.send_email(
            FromEmailAddress="ritual@joeytan.dev",
            Destination={"ToAddresses": [deliverer]},
            Content={
                "Simple": {
                    "Subject": {"Data": f"{today} Activities Logged"},
                    "Body": {
                        "Html": {
                            "Data": markdown.markdown(
                                oai_response.choices[0].message.content
                            )
                        }
                    },
                }
            },
        )

        return "success", 200
    except Exception as e:
        print(f"error: {str(e)}")

        ses_client.send_email(
            FromEmailAddress="ritual@joeytan.dev",
            Destination={"ToAddresses": [deliverer, "j.tan2231@gmail.com"]},
            Content={
                "Simple": {
                    "Subject": {"Data": "Error Logging Activities"},
                    "Body": {
                        "Text": {
                            "Data": f"The followed activities failed to log:\n{pprint.pformat(activities)}\n\nError: {str(e)}\n\Email content:\n{html_content}"
                        }
                    },
                }
            },
        )

        return str(e), 400


@app.route("/")
def serve():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(use_reloader=True, port=5000)
