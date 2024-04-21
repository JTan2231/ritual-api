import json
import os
import pprint
import re
import secrets
from datetime import date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from functools import wraps

import boto3
import markdown2 as markdown
from flask import Flask, redirect, request, send_from_directory
from flask_apscheduler import APScheduler
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI

openai_client = OpenAI()
ses_client = boto3.client("sesv2")

app = Flask(__name__, static_folder="build", static_url_path="/")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["RITUAL_DB_URL"]
app.config["SCHEDULER_API_ENABLED"] = True

scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

CORS(app)

db = SQLAlchemy(app)

TEMPERATURE = 0.01
DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%H:%M:%S"
DATETIME_FORMAT = f"{DATE_FORMAT} {TIME_FORMAT}"

GPT_MODEL = "gpt-4-turbo"


class EthosDefault:
    core = "A friendly, helpful partner focused on the routines, rituals, and personal growth of the user."


with open("prompts/feedback.txt", "r") as f:
    EthosDefault.feedback = f.read()

with open("prompts/summary.txt", "r") as f:
    EthosDefault.summary = f.read()


class User(db.Model):
    user_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(256), nullable=False, unique=True)
    password = db.Column(db.String(256), nullable=False)
    active = db.Column(db.Boolean, default=False, nullable=False)
    last_active = db.Column(db.DateTime, default=datetime.now, nullable=False)
    receiving_logs = db.Column(db.Boolean, default=False, nullable=False)


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


class Token(db.Model):
    token_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    data = db.Column(db.String(256), nullable=False)
    creation_date = db.Column(db.DateTime, nullable=False)


# for actions performed through email
# POST requests _only_
# `username` must be included in the JSON body of the request
# sets user_id in request
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


# generic token authentication
# requires only { Authorization: `Basic ${token}` } header
# sets user_id in request
def token_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization")
        if auth is None:
            return "Unauthorized", 401
        else:
            type, token = auth.split(" ", 1)
            type = type.lower()
            if type != "bearer":
                return "Unauthorized", 401

            user = User.query.join(Token).filter(Token.data == token).first()

            if user is not None:
                request.user_id = user.user_id
                return func(*args, **kwargs)
            else:
                return "Unauthorized", 401

    return wrapper


def set_user_active(user_id):
    user = User.query.filter_by(user_id=user_id).first()
    print(f"updating the activity of user '{user.username}'")
    user.active = True
    user.last_active = datetime.now()


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


def get_activity_html_string(activities):
    days = {}
    for a in activities:
        if a.activity_date not in days:
            days[a.activity_date] = []

        days[a.activity_date].append(a)

    html = '<h1 style="font-family: Helvetica;">Your Activities This Past Week</h1>'
    html += '<div style="font-family: serif;">'

    day_lists = []
    for day, day_activities in days.items():
        day_html = f'<h2 style="font-family: Helvetica;">{day}</h2>'
        day_html += "<ul>"
        for a in day_activities:
            day_html += f"<li><b>{a.name}</b> - {a.memo}"

        day_html += "</ul>"

        day_lists.append((day, day_html))

    day_lists = sorted(day_lists, key=lambda x: x[0], reverse=True)

    for _, day_html in day_lists:
        html += day_html

    html += "</div>"

    return html


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


# makes datetime with current date
def time_to_datetime(time_string):
    if time_string is None or time_string == "":
        return ""

    return datetime.combine(
        date.today(), datetime.strptime(time_string, TIME_FORMAT).time()
    ).strftime(DATETIME_FORMAT)


def openai_prompt(system_prompt, user_prompt):
    print("prompting gpt...")
    oai_response = openai_client.chat.completions.create(
        model=GPT_MODEL,
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {"role": "user", "content": user_prompt},
        ],
    )

    print(f"prompt finished. usage: {oai_response.usage}")

    return oai_response.choices[0].message.content


# TODO: better error handling when this fails
def activity_from_chat(chat):
    oai_response = openai_prompt(
        f'You are an assistant designed to convert conversationally-styled messages describing the user\'s activities and turn them into a list of objects, each object of the following JSON format: {{ "activity_name": "name of activity (string)", "activity_begin": "timestamp ({TIME_FORMAT}) of when the activity began | null", "activity_end": "timestamp ({TIME_FORMAT}) of when the activity ended | null", "memo": "a string describing the details of the activity" }}. Note, your response _must_ be an array of JSON objects--maybe even empty! Pay special attention to the verbs in the message--it is _very_ important that you log _all_ activities listed in the message. _Every_ detail must be accounted for--side thoughts and tangential details are important and must be included! But also! You _must not be redundant_. There _must not_ be activities logged that fit under the umbrella of another. Note: the only contents of the message _must_ be JSON parseable--no markdown or any other similar formatting.',
        chat,
    )

    try:
        activities = json.loads(oai_response)
        print(activities)
        for a in activities:
            a["activity_begin"] = time_to_datetime(a["activity_begin"])
            a["activity_end"] = time_to_datetime(a["activity_end"])

        return activities
    except Exception as e:
        print(oai_response)
        print("error parsing OAI response: " + str(e))
        return {}


def goals_from_chat(chat):
    oai_response = openai_prompt(
        f'You are an expert assistant designed to convert conversationally-styled messages describing the user\'s goals and turn them into a list of objects, each object of the following JSON format: {{ "name": "string", "description": "string" }}. Note, your response _must_ be an array of JSON objects--maybe even empty! Pay special attention to the verbs in the message--it is _very_ important that you log _all_ goals listed in the message. But also! You _must not be redundant_. There _must not_ be goals logged that fit under the umbrella of another. Note: the only contents of the message _must_ be JSON parseable--no markdown or any other similar formatting. Please note: the chat you receive may be a reply to the email--be careful! You must _only_ pay attention to the replying email--do not pay any mind to the email that is being replied to.',
        chat,
    )

    try:
        goals = json.loads(oai_response)

        return goals
    except Exception as e:
        print(oai_response)
        print("goals_from_chat: error parsing OAI response: " + str(e))
        return {}


def style_email_html(html):
    formatted = (
        '<div style="max-width:600px; margin:auto; padding:20px; font-size:20px; font-family: serif"">'
        + html
        + "</div>"
    )

    return formatted


def generate_subgoals(goal):
    oai_response = openai_prompt(
        "you're a world class coach--of everything. You are opinionated, obsessively creative, and _always_ know exactly what to do in order to achieve a client's goal. Your solutions are _concise_--they never do more than is needed--and are exceedingly precise--they are composed of _very specific_ subtasks that get to the core of the problem and address it entirely. Your words are inspiring and creative. Your guidance is actionable and precise. In this chat, you will be given a goal--you're job is to provide an expertly guided, actionable list of steps to take to accomplish that goal. The client should _never_ walk away with any possible questions on what to do. Distill your list into 5 key points into a JSON array, each object having fields 'name' and a detailed 'description'. Ensure your response is plaintext, without markdown formatting.",
        f"{goal.name} -- {goal.description}",
    )

    return [
        Subgoal(
            user_id=request.user_id,
            goal_id=goal.goal_id,
            name=sg["name"],
            description=sg["description"],
        )
        for sg in json.loads(oai_response)
    ]


def get_text_from_email(email_content):
    html_content = ""

    if email_content.is_multipart():
        for part in email_content.walk():
            if part.get_content_type() == "text/plain":
                html_content += part.get_content()
    else:
        if email_content.get_content_type() == "text/plain":
            html_content += email_content.get_content()

    return html_content


# shorthand for the `ses_client.send_email` function
def send_email(subject, html_content, recipient):
    print(f"sending email '{subject}' to {recipient}")
    print(
        ses_client.send_email(
            FromEmailAddress="ritual@joeytan.dev",
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject},
                    "Body": {"Html": {"Data": style_email_html(html_content)}},
                }
            },
        )
    )


def send_error_email(subject, failed_objects, error, email_content, recipient):
    send_email(
        subject,
        f"The following goals/subgoals failed to log:\n{pprint.pformat(failed_objects)}\n\nError: {str(error)}\n\Email content:\n{email_content}",
        recipient,
    )

    send_email(
        subject,
        f"The following goals/subgoals failed to log:\n{pprint.pformat(failed_objects)}\n\nError: {str(error)}\n\Email content:\n{email_content}",
        "j.tan2231@gmail.com",
    )


@app.route("/newsletter-signup", methods=["POST"])
def newsletter_signup():
    email = request.json["email"]

    print(f"newsletter_signup called for email '{email}'")

    user = User.query.filter_by(username=email).first()
    if user is not None:
        return "This user is already registered", 409

    try:
        db.session.add(User(username=email, password=secrets.token_hex(32)))
        db.session.commit()

        print(f"user created for {email}")

        with open("onboarding.html", "r") as f:
            print(f"sending onboarding email to {email}")
            ses_client.send_email(
                FromEmailAddress="ritual@joeytan.dev",
                Destination={"ToAddresses": [email]},
                Content={
                    "Simple": {
                        "Subject": {"Data": "Welcome to Ritual!"},
                        "Body": {"Html": {"Data": f.read()}},
                    }
                },
                ReplyToAddresses=["ritual@joeytan.dev"],
            )

        return "Success", 200
    except Exception as e:
        print(f"error: {str(e)}")

        return str(e), 400


# NOTE: This resets the user's goals--will this be a permanent feature?
@app.route("/onboarding", methods=["POST"])
@email_auth
def onboarding():
    deliverer = request.json["username"]
    print(f"onboarding started for user '{deliverer}'")

    def extract_latest_message(email_data):
        msg = BytesParser(policy=policy.default).parsebytes(email_data)

        def find_latest_text_part(message):
            if message.is_multipart():
                for part in reversed(message.get_payload()):
                    text = find_latest_text_part(part)
                    if text:
                        return text
            else:
                if message.get_content_type() == "text/html":
                    return message.get_payload(decode=True).decode("utf-8")

            return None

        return find_latest_text_part(msg)

    existing_goals = (
        Goal.query.filter_by(user_id=request.user_id).all()
        + Subgoal.query.filter_by(user_id=request.user_id).all()
    )
    for eg in existing_goals:
        db.session.delete(eg)

    if len(existing_goals) > 0:
        print(f"deleting {len(existing_goals)} preexisting goals")
        db.session.commit()

    html_content = extract_latest_message(request.json["email_data"].encode("utf-8"))

    onboarding_tag = r'id=".*onboarding_tag.*"'
    match = re.search(onboarding_tag, html_content)
    if match is None:
        # TODO: Better error handling
        return "Error: missing onboarding tag", 400

    html_content = html_content[: match.start()]

    creation_receipt = []
    goal_json = goals_from_chat(html_content)

    goals = []
    for g in goal_json:
        goal = Goal(
            user_id=request.user_id,
            name=g["name"],
            description=g["description"],
        )

        goals.append(goal)
        db.session.add(goal)

    db.session.commit()

    subgoal_count = 0
    for goal in goals:
        subgoals = generate_subgoals(goal)
        subgoal_count += len(subgoals)

        creation_receipt.append({"goal": goal, "subgoals": subgoals})
        db.session.add_all(subgoals)

    try:
        set_user_active(request.user_id)

        db.session.commit()

        print(f"created {len(goals)} goals and {subgoal_count} subgoals")

        response = '<h1 style="font-family: Helvetica;">New Goals Set</h1>'
        for g in creation_receipt:
            goal = g["goal"]
            subgoals = g["subgoals"]
            response += f'<h2 style="font-family: Helvetica;">{goal.name} -- {goal.description}</h2><ul>'
            for sg in subgoals:
                response += f'<li style="margin-bottom: 0.5rem; font-family: serif;"><b>{sg.name}</b> -- {sg.description}'

            response += "</ul>"

        send_email("New Goals Set", response, deliverer)

        return "success", 200
    except Exception as e:
        print(f"error: {str(e)}")

        send_error_email(
            "Error Setting Goals", creation_receipt, e, html_content, deliverer
        )

        return str(e), 400


@app.route("/email-log-activities", methods=["POST"])
@email_auth
def email_log_activities():
    deliverer = request.json["username"]
    print(f"logging activities by email from {deliverer}")

    msg = BytesParser(policy=policy.default).parsebytes(
        request.json["email_data"].encode("utf-8")
    )

    html_content = get_text_from_email(msg)
    today = datetime.now().strftime("%Y-%m-%d")

    print(html_content)

    def sanitize_date(date):
        return date if len(date) > 0 else None

    activity_json = activity_from_chat(html_content)
    activities = [
        Activity(
            user_id=request.user_id,
            name=a["activity_name"],
            activity_begin=sanitize_date(a["activity_begin"]),
            activity_end=sanitize_date(a["activity_end"]),
            activity_date=today,
            memo=a["memo"],
        )
        for a in activity_json
    ]

    db.session.add_all(activities)

    try:
        set_user_active(request.user_id)
        db.session.commit()

        print(f"committed {len(activities)} activities")

        today_datetime = datetime.strptime(today, DATE_FORMAT)
        activities = (
            Activity.query.filter(
                Activity.activity_date.between(
                    today_datetime - timedelta(days=7), today_datetime
                )
            )
            .filter_by(user_id=request.user_id)
            .all()
        )

        activities_html = get_activity_html_string(activities)

        send_email(
            f"{today} Activities Logged",
            activities_html,
            deliverer,
        )

        return "success", 200
    except Exception as e:
        print(f"error: {str(e)}")

        send_error_email(
            "Error Logging Activities", activities, e, html_content, deliverer
        )

        return str(e), 400


@app.route("/send-newsletters", methods=["POST"])
@email_auth
def send_newsletters():
    end_date = datetime.now()
    begin_date = end_date - timedelta(days=7)

    users = User.query.filter_by(active=True).all()
    print(f"sending newsletters to {[u.username for u in users]}")
    for user in users:
        activities = Activity.query.filter(
            Activity.activity_date >= begin_date,
            Activity.activity_date <= end_date,
            Activity.user_id == user.user_id,
        ).all()

        goals = Goal.query.filter_by(user_id=user.user_id).all()
        subgoals = Subgoal.query.filter(
            Subgoal.goal_id.in_([g.goal_id for g in goals]),
            Subgoal.user_id == user.user_id,
        )

        ethos = get_ethos()
        activities_and_goals = format_activities_and_goals(activities, goals, subgoals)
        oai_response = openai_prompt(ethos.summary, activities_and_goals)

        html = markdown.markdown(oai_response)
        for tag in ("<h1>", "<h2>", "<h3>", "<h4>"):
            html = html.replace(tag, tag[:-1] + ' style="font-family: Helvetica;">')

        send_email(
            f'Ritual Weekly Report {end_date.strftime("%m/%d").lstrip("0").replace("/0", "/")}',
            html,
            user.username,
        )

    return "success", 200


@app.route("/get-config-token", methods=["GET"])
def user_config():
    username = request.args.get("email", None)
    if username is None:
        return "username not found", 400

    user = User.query.filter_by(username=username).first()
    token = Token(user_id=user.user_id, data=secrets.token_urlsafe(16))
    db.session.add(token)
    db.session.commit()

    return redirect(f"https://joeytan.dev/ritual_configuration?token={token.data}")


@app.route("/update-settings", methods=["POST"])
@token_auth
def update_settings():
    if request.json["delete_user"]:
        print(f"deleting user id {request.user_id}")
        user_data = (
            Activity.query.filter_by(user_id=request.user_id).all()
            + Subgoal.query.filter_by(user_id=request.user_id).all()
            + Goal.query.filter_by(user_id=request.user_id).all()
            + User.query.filter_by(user_id=request.user_id).all()
        )

        print(f"deleting {len(user_data)} items associated with user {request.user_id}")

        email = user_data[-1].username

        for data in user_data:
            db.session.delete(data)

        try:
            db.session.commit()

            send_email(
                "User Deleted",
                "Your account and all of its associated data has been deleted.\n\nThank you for using Ritual.",
                email,
            )

            return "success", 200
        except Exception as e:
            print(f"update_settings error: {e}")

            send_error_email(
                "Error Deleting User",
                f"There was an error deleting user {email}. Please contact j.tan2231@gmail.com to get this issue resolved.",
                email,
            )

            send_error_email(
                "Error Deleting User",
                f"There was an error deleting user {email}. Please contact j.tan2231@gmail.com to get this issue resolved.",
                "j.tan2231@gmail.com",
            )

            return "error", 400

    user = User.query.filter_by(user_id=request.user_id).first()
    user.receiving_logs = request.json["receiving_logs"]
    user.active = request.json["receiving_newsletters"]

    try:
        db.session.commit()

        print(
            f"updated user {user.username} with the following settings: {request.json}"
        )

        send_email(
            "Updated Account Settings",
            f"Your account settings have been updated to reflect the following values:<ul><li>Receiving newsletters: <b>{user.active}</b><li>Receiving log receipts: <b>{user.receiving_logs}</b></ul>",
            user.username,
        )

        return "success", 200
    except Exception as e:
        print(f"update_settings error: {e}")

        return "error", 400


@scheduler.task("cron", id="user_last_active_check", hour=18, minute=0)
def user_last_active_check():
    print("running `user_last_active_check`")
    limit = datetime.now() - timedelta(days=4)
    with app.app_context():
        users = User.query.filter(User.last_active < limit, User.active).all()
        for i in range(len(users)):
            users[i].active = False

        db.session.commit()

    print(f"end `user_last_active_check` -- updated {len(users)} users")


@app.route("/")
def serve():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(use_reloader=True, port=5000)
