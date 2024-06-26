import os
import pprint
import random
import re
import secrets
import string
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from functools import wraps

import bcrypt
import boto3
import markdown2 as markdown
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory
from flask_apscheduler import APScheduler
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
from pinecone import Pinecone

openai_client = OpenAI()
ses_client = boto3.client("sesv2")
pinecone_client = Pinecone(api_key=os.environ["PINECONE_API_KEY"])

pc_index = pinecone_client.Index("ritual")
memory_index = pinecone_client.Index("ritual-memory")

app = Flask(__name__, static_folder="build", static_url_path="/")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["RITUAL_DB_URL"]
app.config["SCHEDULER_API_ENABLED"] = True

scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

CORS(app)

db = SQLAlchemy(app)

TEMPERATURE = 0.99
DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%H:%M:%S"
DATETIME_FORMAT = f"{DATE_FORMAT} {TIME_FORMAT}"

GPT_MODEL = "gpt-4o"


class EthosDefault:
    core = "A friendly, helpful partner focused on the routines, rituals, and personal growth of the user."


with open("prompts/summary.txt", "r") as f:
    EthosDefault.summary = f.read()


class Emotions:
    HAPPINESS = "Here's an example of the user feeling happiness."
    SADNESS = "Here's an example of the user feeling sadness."
    ANGER = "Here's an example of the user feeling anger."
    FEAR = "Here's an example of the user feeling fear."
    EXCITEMENT = "Here's an example of the user feeling excitement."
    LONELINESS = "Here's an example of the user feeling loneliness."
    CALMNESS = "Here's an example of the user feeling calmness."
    ANXIETY = "Here's an example of the user feeling anxiety."
    LOVE = "Here's an example of the user feeling love."
    CONFUSION = "Here's an example of the user feeling confusion."
    GRATITUDE = "Here's an example of the user feeling gratitude."
    JEALOUSY = "Here's an example of the user feeling jealousy."
    RELIEF = "Here's an example of the user feeling relief."
    EMBARRASSMENT = "Here's an example of the user feeling embarrassment."
    BOREDOM = "Here's an example of the user feeling boredom."
    PRIDE = "Here's an example of the user feeling pride."
    GUILT = "Here's an example of the user feeling guilt."
    SHAME = "Here's an example of the user feeling shame."
    HOPE = "Here's an example of the user feeling hope."
    DISAPPOINTMENT = "Here's an example of the user feeling disappointment."
    NOSTALGIA = "Here's an example of the user feeling nostalgia."
    SYMPATHY = "Here's an example of the user feeling sympathy."
    FRUSTRATION = "Here's an example of the user feeling frustration."
    CONTENTMENT = "Here's an example of the user feeling contentment."

    @classmethod
    def list_emotions(cls):
        return [
            value
            for name, value in cls.__dict__.items()
            if not name.startswith("__")
            and name != "list_emotions"
            and not callable(value)
        ]


class User(db.Model):
    user_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(256), nullable=False, unique=True)
    password = db.Column(db.String(256), nullable=False)
    active = db.Column(db.Boolean, default=False, nullable=False)
    last_active = db.Column(db.DateTime, default=datetime.now, nullable=False)
    receiving_logs = db.Column(db.Boolean, default=True, nullable=False)
    test_user = db.Column(db.Boolean, default=False, nullable=False)
    archiving = db.Column(db.Boolean, default=True, nullable=False)
    cli_secret = db.Column(db.String(256))
    web_secret = db.Column(db.String(256))
    last_newsletter = db.Column(db.DateTime, default=datetime.now, nullable=False)


class Email(db.Model):
    email_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"))
    raw_email = db.Column(db.Text, nullable=False)
    creation_date = db.Column(db.DateTime, default=datetime.now)
    imported_data = db.Column(db.Boolean, default=False)


class Ethos(db.Model):
    ethos_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    core = db.Column(db.String(4096), nullable=False)
    summary = db.Column(db.String(4096))
    feedback = db.Column(db.String(4096))


class Token(db.Model):
    token_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    data = db.Column(db.String(256), nullable=False)
    creation_date = db.Column(db.DateTime, default=datetime.now, nullable=False)


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


# only allows requests within certain time window (Sunday, 7:45 - 8:15 CST)
# times here are in GMT
def cli_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        now = datetime.now()
        if (
            (now.hour == 12 and now.minute < 45)
            or (now.hour == 13 and now.minute > 15)
            or now.hour < 12
            or now.hour > 13
            or now.weekday() != 6
        ):
            return "Unauthorized", 401

        auth = request.headers.get("Authorization")
        if auth is None:
            return "Unauthorized", 401
        else:
            type, secret = auth.split(" ", 1)
            type = type.lower()
            if type != "bearer":
                return "Unauthorized", 401

            user = User.query.filter(User.cli_secret == secret).first()

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

    return user


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


def style_email_html(html, recipient, format=True):
    formatted = ""
    if format:
        formatted = (
            '<div style="max-width:600px; margin:auto; padding:20px; font-size:20px; font-family: serif">'
            + html
            + f'<p style="font-size: 14px; padding-top: 2rem;">If you would like to unsubscribe or customize your account settings, <a href="https://ritual-api-production.up.railway.app/get-config-token?email={recipient}">click here</a>.</p>'
            + "</div>"
        )
    else:
        formatted = (
            '<div style="max-width: 600px; margin: auto; padding: 20px; font-size: 16px; font-family: Helvetica;">'
            + "Use this link to change your account settings: "
            + html
            + " Do not share this link with anybody. This link expires 15 minutes after its creation.</div>"
        )

    return formatted


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
def send_email(subject, html_content, recipient, format=True):
    print(f"sending email '{subject}' to {recipient}")
    print(
        ses_client.send_email(
            FromEmailAddress="ritual@joeytan.dev",
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject},
                    "Body": {
                        "Html": {
                            "Data": (style_email_html(html_content, recipient, format))
                        }
                    },
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


# get all user entries within the past `days` days
def get_user_entries_in_range(user_id, days):
    emails = Email.query.filter(
        Email.user_id == user_id,
        Email.creation_date > datetime.now() - timedelta(days=days),
    ).all()

    return emails


def get_db_email_text(email_object):
    msg = BytesParser(policy=policy.default).parsebytes(
        email_object.raw_email.encode("utf-8")
    )
    return get_text_from_email(msg)


# formatted in a string for GPT
def format_emails_for_gpt(emails):
    formatted_string = ""
    for e in emails:
        formatted_string += f"{e.creation_date} -- "
        if e.imported_data:
            formatted_string += e.raw_email
        else:
            formatted_string += get_db_email_text(e)

        formatted_string += "\n\n"

    return formatted_string if len(formatted_string) > 0 else "No emails logged."


def get_embedding(text):
    return (
        openai_client.embeddings.create(input=text, model="text-embedding-3-small")
        .data[0]
        .embedding
    )


# get oai embedding for `text`
# then find most similar quote
def get_quote(text):
    embedding = get_embedding(text)

    responses = [
        x["metadata"]
        for x in pc_index.query(
            vector=embedding,
            top_k=5,
            include_metadata=True,
            filter={"author": {"$ne": "Frank Herbert"}},
        )["matches"]
    ]

    return random.choice(responses)


# @app.route("/analyze-emails", methods=["POST"])
def analyze_emails():
    emails = get_user_entries_in_range(1, 30)
    ids = [e.email_id for e in emails]

    counts = {}
    for emotion in Emotions.list_emotions():
        print(f"analyzing for {emotion}")
        emotion_embedding = get_embedding(emotion)

        closest = [
            (r["score"], r["metadata"]["email_id"])
            for r in memory_index.query(
                vector=emotion_embedding,
                top_k=1,
                include_metadata=True,
                filter={"email_id": {"$in": ids}},
            )["matches"][:1]
        ][0]

        counts[emotion] = closest

    total = 0
    for emotion, email_id in counts.items():
        # email = Email.query.filter_by(email_id=email_id[1]).first()
        print(f"{emotion}, {email_id[0]}: {email_id[1]}")
        print("------------")

        total += email_id[0]

    print(f"average score: {total / len(counts)}")


def get_color(formatted_email_text):
    openai_response = openai_prompt(
        "Assign a hex color code representing the mood of the user's input. Ensure these colors are subtle and off-colored, gently guiding the user's subconscious to the desired tone and mood. Respond _only_ with the hex code.",
        formatted_email_text,
    )

    hex_code = "#FFFFFF"

    hex_regex = r"#[0-9a-fA-F]{6}"
    if re.match(hex_regex, openai_response):
        hex_code = re.search(hex_regex, openai_response).group(0)
    else:
        print('color response not valid, defaulting to "#FFFFFF"')
        print('response: "' + openai_response + '"')

    hex_code = hex_code[1:]
    # subtracting from 255 to treat it as origin
    rgb = tuple(int(hex_code[i : i + 2], 16) for i in (0, 2, 4))

    radius = 80

    # normalize to within radius
    magnitude = sum((255 - rgb[i]) ** 2 for i in range(3)) ** 0.5

    if magnitude > radius:
        rgb = tuple(255 - int((255 - rgb[i]) * radius / magnitude) for i in range(3))

    hex_code = "#" + "".join(f"{x:02x}" for x in rgb)

    return hex_code


def create_user(email):
    user = User.query.filter_by(username=email).first()
    if user is not None:
        return "This user is already registered", 409

    user = User(username=email, password=secrets.token_hex(32))
    db.session.add(user)
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

    return user


@app.route("/newsletter-signup", methods=["POST"])
def newsletter_signup():
    email = request.json["email"]

    print(f"newsletter_signup called for email '{email}'")
    try:
        create_user(email)
    except Exception as e:
        print(f"error: {str(e)}")

        return str(e), 400


@app.route("/email-log-activities", methods=["POST"])
@email_auth
def email_log_activities():
    deliverer = request.json["username"]
    print(f"logging activities by email from {deliverer}")

    email = Email(
        user_id=request.user_id,
        raw_email=request.json["email_data"],
        imported_data=False,
    )

    db.session.add(email)

    try:
        user = set_user_active(request.user_id)
        db.session.commit()

        emails = get_user_entries_in_range(request.user_id, 7)

        days = {e.creation_date.strftime(DATE_FORMAT): [] for e in emails}
        for e in emails:
            days[e.creation_date.strftime(DATE_FORMAT)].append(get_db_email_text(e))

        days = sorted(
            [(key, value) for key, value in days.items()],
            key=lambda x: x[0],
            reverse=True,
        )

        receipt_html = ""
        for key, value in days:
            receipt_html += f"<p><h2>{key}</h2>{'<hr>'.join(value)}</p>"

        if user.receiving_logs:
            send_email(
                f"{datetime.now().strftime(DATE_FORMAT)} Activities Logged",
                receipt_html,
                deliverer,
            )

        embedding = get_embedding(request.json["email_data"])
        print(
            memory_index.upsert(
                [
                    {
                        "id": "".join(
                            secrets.choice(string.ascii_letters + string.digits)
                            for _ in range(32)
                        ),
                        "values": embedding,
                        "metadata": {
                            "user_id": request.user_id,
                            "email_id": email.email_id,
                        },
                    }
                ]
            )
        )

        return "success", 200
    except Exception as e:
        print(f"error: {str(e)}")

        send_error_email(
            "Error Logging Activities", [], e, request.json["email_data"], deliverer
        )

        return str(e), 400


def get_newsletter(formatted_email_text):
    # can we do this without prompting gpt twice?
    oai_response = openai_prompt(EthosDefault.summary, formatted_email_text)

    html = markdown.markdown(oai_response)
    for tag in ("<h1>", "<h2>", "<h3>", "<h4>"):
        html = html.replace(tag, tag[:-1] + ' style="font-family: Helvetica;">')

    max_len = 500
    quote_data = get_quote(formatted_email_text)
    color = get_color(quote_data["text"][:max_len])

    # find earliest punctuation after the `max_len` char mark and cut off
    if len(quote_data["text"]) > max_len:
        mark = max_len
        for i, c in enumerate(quote_data["text"][max_len:]):
            if c in (";", ",", ".", "/"):
                mark += i
                break

        quote_data["text"] = quote_data["text"][:mark] + "..."

    html = (
        '<div style="background-color: transparent; margin: auto; padding: 20px;">'
        '<blockquote style="margin-bottom: 2em"><p style="font-size: 1.1rem"><i>'
        + quote_data["text"]
        + f'</i></p><cite style="font-size: 1rem">— {quote_data["author"]}, {quote_data["title"]}</cite></blockquote><hr>'
        + html
        + "</div>"
    )

    return html, color


def jsonify_html(html_string):
    soup = BeautifulSoup(html_string, "html.parser")

    def element_to_dict(element):
        if element.name is None:
            return {"tag": "text", "attributes": {}, "text": element.string.strip()}

        element_dict = {
            "tag": element.name,
            "attributes": element.attrs,
            "children": [
                element_to_dict(child)
                for child in element.children
                if child.name or child.string.strip()
            ],
        }

        return element_dict

    return element_to_dict(soup)


def get_exa_webpages(email_text):
    def exa_post(url, headers, body, result_key):
        response = requests.post(url, headers=headers, json=body)
        if response.status_code != 200:
            print(f"error: exa.ai request failed -- {response.text}")
            if "API key usage limit reached" not in response.text:
                print('url: "' + url + '"')
                print('body: "' + str(body) + '"')
            return []

        return response.json()[result_key]

    query = email_text + "\n---\nHere's a link most relevant to the above entry: "

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-key": os.environ["EXAI_API_KEY"],
    }

    body = {"query": query, "category": "personal site"}

    webpages = exa_post("https://api.exa.ai/search", headers, body, "results")

    # make a random selection from the top N results
    webpages = sorted(webpages, key=lambda x: x["score"], reverse=True)[:30]
    webpages = random.sample(webpages, min(10, len(webpages)))
    page_ids = [x["id"] for x in webpages]

    contents_body = {"ids": page_ids, "text": {"maxCharacters": 512}}

    page_contents = exa_post(
        "https://api.exa.ai/contents", headers, contents_body, "results"
    )

    return [
        {"url": x["url"], "title": x["title"], "text": x["text"]} for x in page_contents
    ]


@app.route("/send-newsletters", methods=["POST"])
@email_auth
def send_newsletters():
    end_date = datetime.now()

    users = User.query.filter_by(active=True, user_id=1).all()
    print(f"sending newsletters to {[u.username for u in users]}")

    successes = []
    for user in users:
        try:
            emails = get_user_entries_in_range(user.user_id, 7)

            webpages = []
            for e in emails:
                webpages += get_exa_webpages(get_db_email_text(e))

            # dirty way to get rid of duplicate urls
            webpages = {w["url"]: w for w in webpages}

            formatted_email_text = format_emails_for_gpt(emails)
            if len(emails) > 0:
                memory_embedding = get_embedding(formatted_email_text)

                memory_ids = [
                    x["metadata"]["email_id"]
                    for x in memory_index.query(
                        vector=memory_embedding,
                        top_k=3,
                        include_metadata=True,
                        filter={
                            "user_id": {"$eq": emails[0].user_id},
                            "email_id": {"$nin": [e.email_id for e in emails]},
                        },
                    )["matches"]
                ]

                memories = Email.query.filter(Email.email_id.in_(memory_ids)).all()

                formatted_email_text += "--- Memories ---\n\n"
                for m in memories:
                    formatted_email_text += get_db_email_text(m) + "\n---\n"

            formatted_email_text += "--- Webpages ---\n\n"
            for w in webpages.values():
                formatted_email_text += (
                    f"{w['title']} -- {w['url']}\n{w['text']}\n---\n"
                )

            newsletter, _ = get_newsletter(formatted_email_text)

            send_email(
                f'Ritual Weekly Report {end_date.strftime("%m/%d").lstrip("0").replace("/0", "/")}',
                newsletter,
                user.username,
            )

            user.last_newsletter = datetime.now()

            if not user.archiving:
                successes += emails
        except Exception as e:
            print(f"error generating newsletter for {user.username}: {e}")

    try:
        for email in successes:
            db.session.delete(email)

        db.session.commit()
    except Exception as e:
        print(
            f"error deleting email id {email.email_id} for user id {email.user_id}: {e}"
        )

    return "success", 200


@app.route("/cli-newsletters", methods=["POST"])
@cli_auth
def cli_newsletters():
    # get and format any emails from the last 7 days
    emails = get_user_entries_in_range(request.user_id, 7)
    formatted_email_text = format_emails_for_gpt(emails)
    formatted_email_text += "\n\n".join(
        [f"{e['date']} -- {e['text']}" for e in request.json["entries"]]
    )

    user = User.query.filter_by(user_id=request.user_id).first()
    print(f"preparing email for user {user.username}")

    try:
        send_email(
            f'Ritual Weekly Report {datetime.now().strftime("%m/%d").lstrip("0").replace("/0", "/")}',
            get_newsletter(formatted_email_text)[0],
            user.username,
        )

        user.last_newsletter = datetime.now()

        db.session.commit()

        return "", 200
    except Exception as e:
        print(f"error generating/sending newsletter for {user.username}: {e}")

        return "error", 400


def style_config_status(message):
    return f'<div style="position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); font-family: monospace;">{message}</div>'


def create_token(user_id):
    token = Token(user_id=user_id, data=secrets.token_urlsafe(16))

    return token


@app.route("/get-config-token", methods=["GET"])
def user_config():
    username = request.args.get("email", None)
    if username is None:
        return style_config_status("username not found"), 400

    user = User.query.filter_by(username=username).first()
    if user is None:
        print(f"user {username} not found")
        return style_config_status(f"username {username} not found"), 404

    token = create_token(user.user_id)
    db.session.add(token)
    db.session.commit()

    send_email(
        "Update Account Settings",
        f"https://joeytan.dev/ritual_configuration?token={token.data}",
        username,
        format=False,
    )

    return style_config_status(f"Account settings update email sent to {username}"), 200


@app.route("/update-settings", methods=["POST"])
@token_auth
def update_settings():
    if request.json["delete_user"]:
        print(f"deleting user id {request.user_id}")
        user_data = (
            Email.query.filter_by(user_id=request.user_id).all()
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
    user.archiving = not request.json["deleting_data"]

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


@app.route("/generate-cli-token", methods=["GET"])
@token_auth
def generate_cli_token():
    user = User.query.filter_by(user_id=request.user_id).first()
    user.cli_secret = secrets.token_hex(32)

    try:
        db.session.commit()

        return jsonify({"new_secret": user.cli_secret})
    except Exception as e:
        return jsonify({"Error": str(e)}), 400


@app.route("/user-lookup", methods=["GET"])
def user_lookup():
    email = request.args.get("username", None)
    if email is None:
        return "Bad request", 400

    user = User.query.filter_by(username=email).first()

    return jsonify(
        {
            "exists": user is not None and user.web_secret is not None,
        }
    )


@app.route("/web-login", methods=["POST"])
def web_login():
    email = request.json["email"]
    password = request.json["password"]

    user = User.query.filter_by(username=email).first()
    if user is None:
        return "Unauthorized", 401

    if bcrypt.checkpw(password.encode("utf-8"), bytes(user.web_secret, "utf-8")):
        try:
            token = create_token(user.user_id)
            db.session.add(token)
            db.session.commit()

            return jsonify({"token": token.data})
        except Exception as e:
            return str(e), 400

    return "Unauthorized", 401


@app.route("/web-register", methods=["POST"])
def web_register():
    email = request.json["email"]
    password = request.json["password"]

    user = User.query.filter_by(username=email).first()
    if user is None:
        try:
            user = create_user(email)
        except Exception as e:
            print(f"error: {str(e)}")

            return str(e), 400

    user.web_secret = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    token = create_token(user.user_id)
    try:
        db.session.add(token)
        db.session.commit()
    except Exception as e:
        return str(e), 400

    return jsonify({"token": token.data})


@app.route("/web-newsletter", methods=["POST"])
@token_auth
def web_newsletter():
    user = User.query.filter_by(user_id=request.user_id).first()

    latest_sunday = (
        datetime.now() - timedelta(days=datetime.now().weekday() + 1)
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    if user.last_newsletter > latest_sunday:
        print('rate limit reached for user "' + user.username + '"')
        return "Too Many Requests", 429

    print(f"generating newsletter for {user.username}")
    entries = request.json["entries"]

    webpages = []
    for e in entries:
        webpages += get_exa_webpages(e["content"])

    # dirty way to get rid of duplicate urls
    webpages = {w["url"]: w for w in webpages}

    formatted_string = ""
    for e in entries:
        entry_text = f"{e['createdDate']} -- {e['content']}\n\n"
        if len(formatted_string + entry_text) < 7000:
            formatted_string += entry_text
        else:
            break

    formatted_string += "--- Webpages ---\n\n"
    for w in webpages.values():
        webpage_text = f"{w['title']} -- {w['url']}\n{w['text']}\n---\n"
        if len(formatted_string + webpage_text) < 7000:
            formatted_string += webpage_text
        else:
            break

    try:
        newsletter, color = get_newsletter(formatted_string)
        html = jsonify_html(newsletter)

        user.last_newsletter = datetime.now()
        db.session.commit()

        return jsonify(
            {"newsletter": newsletter, "color": color, "jsonified_html": html}
        )
    except Exception as e:
        print(f"error generating/sending newsletter for {user.username}: {e}")

        return "error", 400


@scheduler.task(
    "cron", id="send_test_newsletters", day_of_week="sat", hour=21, minute=15
)
def send_test_newsletters():
    end_date = datetime.now()

    with app.app_context():
        users = User.query.filter_by(test_user=True).all()
        print(f"sending test newsletters to {[u.username for u in users]}")
        for user in users:
            try:
                emails = get_user_entries_in_range(user.user_id, 7)
                formatted_email_text = format_emails_for_gpt(emails)
                send_email(
                    f'{{TESTING}} Ritual Weekly Report {end_date.strftime("%m/%d").lstrip("0").replace("/0", "/")}',
                    get_newsletter(formatted_email_text)[0],
                    user.username,
                )
            except Exception as e:
                print(f"error generating newsletter for {user.username}: {e}")


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


# this should really be abstracted to a separate function
# p much a complete copy+paste from `send_newsletters`
#
# "cleanup"/followup function for users that are also using the CLI
# in case their cron job fails
@scheduler.task(
    "cron", id="send_remaining_newsletters", day_of_week="sun", hour=13, minute=30
)
def send_remaining_newsletters():
    with app.app_context():
        end_date = datetime.now()

        yesterday = datetime.now() - timedelta(days=1)
        # if the user hasn't received a newsletter yet today
        users = User.query.filter(
            User.active == True, User.last_newsletter < yesterday
        ).all()
        print(f"sending remaining newsletters to {[u.username for u in users]}")

        successes = []
        for user in users:
            try:
                emails = get_user_entries_in_range(user.user_id, 7)
                formatted_email_text = format_emails_for_gpt(emails)
                if len(emails) > 0:
                    memory_embedding = get_embedding(formatted_email_text)

                    memory_ids = [
                        x["metadata"]["email_id"]
                        for x in memory_index.query(
                            vector=memory_embedding,
                            top_k=3,
                            include_metadata=True,
                            filter={
                                "user_id": {"$eq": emails[0].user_id},
                                "email_id": {"$nin": [e.email_id for e in emails]},
                            },
                        )["matches"]
                    ]

                    memories = Email.query.filter(Email.email_id.in_(memory_ids)).all()

                    formatted_email_text += "--- Memories ---\n\n"
                    for m in memories:
                        formatted_email_text += get_db_email_text(m) + "\n---\n"

                send_email(
                    f'Ritual Weekly Report {end_date.strftime("%m/%d").lstrip("0").replace("/0", "/")}',
                    get_newsletter(formatted_email_text)[0],
                    user.username,
                )

                user.last_newsletter = datetime.now()

                if not user.archiving:
                    successes += emails
            except Exception as e:
                print(f"error generating newsletter for {user.username}: {e}")

        try:
            for email in successes:
                db.session.delete(email)

            db.session.commit()
        except Exception as e:
            print(
                f"error deleting email id {email.email_id} for user id {email.user_id}: {e}"
            )

        return "success", 200


@scheduler.task("interval", id="clean_tokens", seconds=900, misfire_grace_time=900)
def clean_tokens():
    print("running `clean_tokens`")
    limit = datetime.now() - timedelta(minutes=30)
    with app.app_context():
        tokens = Token.query.filter(Token.creation_date < limit).all()
        for token in tokens:
            db.session.delete(token)

        db.session.commit()

    print(f"end `clean_tokens` -- updated {len(tokens)} users")


@app.route("/")
def serve():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(use_reloader=True, port=5000)
