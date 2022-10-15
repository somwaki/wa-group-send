import logging
import re
import time
from random import randrange
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from yaml import load

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader

from datetime import datetime, timedelta
import json
import os
from typing import List
from flask import Flask, request, render_template, redirect, url_for, flash
import flask_login
import requests
from flask_rq2 import RQ
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

LOG_FORMAT = ' - %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

app = Flask(__name__)

app.secret_key = os.environ.get('APP_SECRET_KEY', "SECRET-KEY-HERE")

# rq configs
app.config['RQ_REDIS_URL'] = os.environ.get('RQ_REDIS_URL', 'redis://localhost:6379/0')
rq = RQ()
rq.init_app(app)

# db configs
db = SQLAlchemy()
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('SQLALCHEMY_DATABASE_URI', "sqlite:///wa.db")
db.init_app(app)


class GroupLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    link = db.Column(db.String(150), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.now())
    chat_id = db.Column(db.String(100), default=None)
    name = db.Column(db.String(250), default=None)


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    has_run = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.now())
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    active = db.Column(db.Boolean, default=True)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey(
        'campaign.id'), nullable=False)
    group_link = db.Column(db.Integer, db.ForeignKey(
        'group_link.id'), nullable=False)
    sent_at = db.Column(db.DateTime(timezone=True), default=datetime.now())
    join_succeeded = db.Column(db.Boolean)
    message_send_succeeded = db.Column(db.Boolean)
    response_dump = db.Column(db.Text)
    updated = db.Column(db.DateTime(timezone=True), onupdate=datetime.now())


with app.app_context():
    db.create_all()


# ------------------------- helper functions --------------------------
def find_links(string):
    # will extract all whatsapp links inside a text message and return as list
    regex = r"(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'\".,<>?«»“”‘’]))"
    url = re.findall(regex, string)
    return [x[0] for x in url]


def get_tld(link):
    parsed_uri = urlparse(link)
    return '{uri.scheme}://{uri.netloc}/'.format(uri=parsed_uri)


# ------------------------- auth -------------------------
login_manager = flask_login.LoginManager()
login_manager.init_app(app)


class User(flask_login.UserMixin):
    pass


users = load(open('users.yaml'), Loader=Loader)


@login_manager.user_loader
def user_loader(username):
    if username not in users:
        return User()

    user = User()
    user.id = username
    return User


@login_manager.request_loader
def request_loader(request):
    username = request.form.get('username')
    if username not in users:
        return User()

    user = User()
    user.id = username
    return User


@login_manager.unauthorized_handler
def unauthorized_handler():
    return redirect(url_for('login'))


@app.route('/')
def home():
    return redirect("dashboard")


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        logger.info(f'USERS: {users}')
        username = request.form['username']
        if username in users and request.form['password'] == users[username]['password']:
            user = User()
            user.id = username
            flask_login.login_user(user)
            return redirect(url_for('dashboard'))
        else:

            flash("Invalid login details Try again")
    if flask_login.current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('app/login.html')


@app.route('/logout')
def logout():
    flask_login.logout_user()
    return redirect(url_for('login'))


# ----------------------------- endpoints -----------------------------

@app.route('/dashboard', methods=['GET'])
@flask_login.login_required
def dashboard():
    # this iframe is the url of the api. once authenticated, the api could redirect to docs page,
    iframe = os.environ.get('API_BASE_URL')
    r = requests.get(iframe, allow_redirects=False)
    if r.status_code == 200:
        return render_template('app/dashboard.html', iframe=iframe)
    else:
        groups = db.session.query(func.count(GroupLink.id))
        campaign_list = db.session.query(func.count(Campaign.id))
        messages = db.session.query(func.count(Message.id))
        summary = {
            'groups': groups.one()[0],
            'campaigns': campaign_list.one()[0],
            'messages': messages.one()[0]
        }
    return render_template('app/dashboard.html', summary=summary)


@app.route('/links', methods=['GET', 'POST'])
@flask_login.login_required
def links():
    if request.method == 'POST':
        group_link = request.form['link']
        links_ = [x for x in find_links(group_link) if get_tld(x) == 'https://chat.whatsapp.com/']
        added = 0
        if len(links_) > 0:
            for item in links_:
                link = GroupLink(link=item)
                db.session.add(link)
                try:
                    db.session.commit()
                    added += 1
                except IntegrityError:
                    db.session.rollback()
                    available_link = db.session.execute(db.select(GroupLink).filter_by(link=item)).scalars().one()
                    if not available_link.active:
                        available_link.active = True
                        db.session.add(available_link)
                        db.session.commit()

        if added > 0:
            flash(f'{added} new link{"" if len(links_) == 1 else "s"} saved successfully')
        else:
            flash(f'No new links were found in the message')
        return redirect(url_for('links'))

    links_list = db.session.execute(
        db.select(GroupLink).filter_by(active=True).order_by(GroupLink.id.desc())).scalars()
    return render_template('app/links.html', links=links_list)


@app.route('/campaigns', methods=['GET', 'POST'])
@flask_login.login_required
def campaigns():
    if request.method == 'POST':
        message = request.form['message']
        title = request.form['title']
        campaign = Campaign(message=message, title=title)
        db.session.add(campaign)
        db.session.commit()
        flash(f'Campaign [{campaign.title}] created successfully')
        return redirect(url_for('campaigns'))

    campaign_list = db.session.execute(
        db.select(Campaign).filter_by(active=True).order_by(Campaign.id.desc())).scalars()
    return render_template('app/campaigns.html', campaigns=campaign_list)


class Whatsapp:
    def __init__(self, url):
        logger.info(f"BASE URL : {url}")
        self.base_url = url

    def send_request(self, endpoint: str, data: dict, method='POST'):

        headers = {
            'accept': "*/*",
            "Content-Type": "application/json"
        }
        try:
            ln = f'{self.base_url}{endpoint}'
            logger.info(f" requesting {ln}...")

            r = requests.request(method, ln, headers=headers, json=data)
        except Exception:
            raise
        logger.info(f" PATH : {endpoint} :: RAW RESPONSE :::>>::: ({r.text})")
        return r.status_code, json.loads(r.text)

    def send_text(self, chat_id: str, message: str):

        payload = {
            'args': {
                'to': chat_id,
                'content': message
            }
        }
        logger.info(f" payload : {payload}")
        return self.send_request(method='POST', endpoint='/sendText', data=payload)

    def join_group(self, link: str):

        payload = {
            'args': {
                'link': link,
                'returnChatObj': 'true'
            }
        }

        logger.info(f" payload : {payload}")
        return self.send_request(method='POST', endpoint='/joinGroupViaLink', data=payload)

    def leave_group(self, chat_id: str):

        payload = {
            "args": {
                "groupId": chat_id
            }
        }

        logger.info(f" payload : {payload}")
        return self.send_request(method='POST', endpoint='/leaveGroup', data=payload)


@rq.job
def join_group(group_link: GroupLink, message: Message, text: str, **kwargs):
    whatsapp = Whatsapp(os.environ.get('API_BASE_URL'))
    logger.info(f" processing join group for {group_link.link}")
    with app.app_context():
        logger.info("joining group")
        code, join_resp = whatsapp.join_group(group_link.link)
        if code == 200 and join_resp["success"] and isinstance(join_resp['response'], dict):
            group_chat_id: str = join_resp["response"].get('id')
            group_name: str = join_resp["response"].get('name')
            logger.info(f" successfully joined group with id: {group_chat_id}")
            if group_chat_id:
                message.join_succeeded = True
                message.response_dump = json.dumps(join_resp)
                group_link.chat_id = group_chat_id
                group_link.name = group_name
                db.session.add(group_link)
                db.session.add(message)
                db.session.commit()
                db.session.refresh(group_link)

                db.session.refresh(group_link)
                db.session.refresh(message)

                # schedule sending message
                secs = randrange(5, 15)
                send_msg_to_group.schedule(
                    timedelta(seconds=secs),
                    whatsapp=whatsapp,
                    group_link=group_link,
                    text=text,
                    message=message)
                logger.info(f" ⏲ scheduled sending message to {group_link.name} for {secs}")
            else:
                group_link.chat_id = None
                message.response_dump = json.dumps(join_resp)
                db.session.add(group_link)
                db.session.add(message)
                db.session.commit()
        else:
            message.join_succeeded = False
            message.response_dump = json.dumps(join_resp)
            group_link.chat_id = None
            db.session.add(group_link)
            db.session.add(message)
            db.session.commit()


@rq.job
def send_msg_to_group(group_link: GroupLink, text: str, message: Message, **kwargs):
    whatsapp = Whatsapp(os.environ.get('API_BASE_URL'))
    with app.app_context():
        logger.info(f" processing send message for {group_link.link} ")
        if group_link.chat_id is not None and message.join_succeeded:
            logger.info(f" sending message to {group_link.name}...")
            send_code, send_resp = whatsapp.send_text(
                chat_id=group_link.chat_id, message=text)
            if send_code == 200 and send_resp['response']:
                message.message_send_succeeded = True
                message.response_dump = json.dumps(send_resp)
                db.session.add(group_link)
                db.session.add(message)
                db.session.commit()
                logger.info("successfully sent message to group")

                db.session.refresh(group_link)
                db.session.refresh(message)

                secs = randrange(1, 10)
                leave_group.schedule(
                    timedelta(seconds=secs),
                    whatsapp=whatsapp,
                    group_link=group_link,
                    message=text)

                logger.info(f" ⏲ scheduled leaving group for {secs}")
            else:
                message.message_send_succeeded = False
                message.response_dump = json.dumps(send_resp)
                db.session.add(group_link)
                db.session.add(message)
                db.session.commit()
                logger.info("Message sending did not succeed")


@rq.job
def leave_group(group_link: GroupLink, message: Message, **kwargs):
    whatsapp = Whatsapp(os.environ.get('API_BASE_URL'))
    with app.app_context():
        logger.info(f" processing leave group for {group_link.link} ")
        exit_code, exit_resp = whatsapp.leave_group(
            chat_id=group_link.chat_id)
        if exit_code == 200:
            logger.info(f"[LEFT GROUP] {exit_resp}")
        message.response_dump = json.dumps(exit_resp)
        db.session.commit()


@rq.job
def campaign_task(links_list: List[GroupLink], message: str, campaign_id, **kwargs):
    """
    sample success response:

    {
        "success": true,
        "response": {
            "id": "120363042118385076@g.us",
            "lastReceivedKey": {
                "fromMe": false,
                "remote": {
                    "server": "g.us",
                    "user": "120363042118385076",
                    "_serialized": "120363042118385076@g.us"
                },
                "id": "3EB09228CB1C1ABF7B25",
                "_serialized": "false_120363042118385076@g.us_3EB09228CB1C1ABF7B25"
                },
                "unreadCount": 0,
                "muteExpiration": 0,
                "hasUnreadMention": false,
                "archiveAtMentionViewedInDrawer": false,
                "hasChatBeenOpened": false,
                "pendingInitialLoading": false,
                "msgs": null,
                "kind": "group",
                "canSend": true,
                "isGroup": true,
                "contact": {
                "id": "120363042118385076@g.us",
                "type": "in",
                "formattedName": "",
                "isMe": false,
                "isMyContact": false,
                "isPSA": false,
                "isUser": false,
                "isWAContact": false,
                "profilePicThumbObj": {
                    "id": "120363042118385076@g.us",
                    "img": null,
                    "imgFull": null
                },
                "msgs": null
                },
                "groupMetadata": {
                    "id": "120363042118385076@g.us",
                    "suspended": false,
                    "terminated": false,
                    "uniqueShortNameMap": {},
                    "participants": [],
                    "pendingParticipants": [],
                    "pastParticipants": [],
                    "membershipApprovalRequests": []
                },
                "presence": {
                "id": {
                    "server": "g.us",
                    "user": "120363042118385076",
                    "_serialized": "120363042118385076@g.us"
                },
                "chatstates": []
            },
            "isOnline": false,
            "participantsCount": 1
        }
    }
    """
    whatsapp = Whatsapp(os.environ.get('API_BASE_URL'))

    for i, link in enumerate(links_list):
        # create message record in database
        msg = Message(campaign_id=campaign_id, group_link=link.id)
        db.session.add(msg)
        db.session.commit()
        db.session.refresh(msg)
        
        """ uncomment the code below and comment the other remaining part to schedule all events. schedule events means,
        for example, 
        after joining a group, a new event to send message to that group is scheduled. and after sucessfully sending, a new event to leave group is scheduled.
        this means, once an event has been scheduled, the code will continue running and processing other events that were scheduled before. that means, you
        could see in your whatsapp, the bot sending a message to one group, leaving other 2 joining other 3 group... leving others sending to others... events
        seem kinda random. but it's good since if one event fails, for example if a message is not sent, the bot will not leave the group since that event will 
        not be scheduled """
        # join_group.schedule(
        #     timedelta(seconds=(i + 1) * 10),
        #     whatsapp=whatsapp,
        #     group_link=link,
        #     message=msg, text=message
        # )
        # logger.info(f" scheduled {link.link} for {(i + 1) * 10} seconds")

        # join group
        
        """if you uncomment the code above, then you should comment everythng else on this function below this quote. When uncommented as it is, the bot
        will perform actions in the same order. join one group, send message, then leave....only then will it go to the next group. if an error occurs it will
        stop there for the group. NOTE:  with this setup, tests we did showed the bot could join a group, and leave without sending the message"""
        code, join_resp = whatsapp.join_group(link.link)
        if code == 200 and join_resp["success"]:
            group_chat_id: str = join_resp["response"]["id"]
            logger.info(f"successfully joined group with id: {group_chat_id}")

            db.session.refresh(msg)
            msg.join_succeeded = True
            db.session.commit()
            if group_chat_id.endswith('@g.us'):

                # send message
                send_code, send_resp = whatsapp.send_text(
                    chat_id=group_chat_id, message=message)
                if send_code == 200 and send_resp['success']:
                    logger.info("successfully sent message to group")

                    db.session.refresh(msg)
                    msg.message_send_succeeded = True
                    msg.response_dump = json.dumps(send_resp)
                    db.session.commit()
                    # update message here

                    time.sleep(2)
                else:
                    db.session.refresh(msg)
                    msg.message_send_succeeded = False
                    msg.response_dump = json.dumps(send_resp)
                    db.session.commit()
                    logger.info(f"message sending DID NOT SUCCEED: {send_resp}")

                # leave group
                if bool(int(os.environ.get('EXIT_GROUPS', False))):
                    time.sleep(10)
                    exit_code, exit_resp = whatsapp.leave_group(
                        chat_id=group_chat_id)
                    if exit_code == 200:
                        logger.info(f"[LEFT GROUP] {exit_resp}")
            else:
                logger.info("malformed link")
        else:
            logger.info("joining group DID NOT SUCCEED")
            db.session.refresh(msg)
            msg.join_succeeded = True
            msg.response_dump = json.dumps(join_resp)
            db.session.commit()

    logger.info(
        f" [campaign:{campaign_id}] DONE PROCESSING ALL LINKS IN THIS CAMPAIGN")


def run_campaign_task(id, **kwargs):
    with app.app_context():
        campaign: Campaign = db.session.execute(
            db.select(Campaign).filter_by(id=id)).scalars().one()
        links = db.session.execute(
            db.select(GroupLink).filter_by(active=True)).scalars().all()

        if campaign:
            logger.info(f"PREPARING TO RUN CAMPAIGN :: {campaign}")
            logger.info(f">>>> :: {campaign.message}")
            campaign_task.queue(links, campaign.message, campaign.id)
            # campaign_task(links, campaign.message, campaign.id)

            campaign.has_run = True
            db.session.add(campaign)
            db.session.commit()
            flash(
                f'Campaign [{campaign.title}] has started running successfully. Check your phone to see the progress.')
        # elif campaign.has_run: logger.info(f"CAMPAIGN :: {campaign} :: HAS ALREADY BEEN RUN") flash(f'Campaign [{
        # campaign.title}] has already been run. If this was a mistake, recreate a new campaign and run.')
        else:
            flash(f'Campaign [{id}] was not found. Please try again later')
            logger.info(f"UNABLE TO FIND CAMPAIGN :: {id}")


@app.route('/campaign/run', methods=['POST'])
@flask_login.login_required
def run_campaign():
    campaign_id = request.form['id']
    run_campaign_task(campaign_id)
    return redirect(url_for('campaigns'))


@app.route('/link/delete', methods=['POST'])
@flask_login.login_required
def delete_link():
    link_id = request.form['id']
    link = db.session.execute(
        db.select(GroupLink).filter_by(id=link_id)).scalars().one()
    link.active = False
    db.session.commit()
    return redirect(url_for('links'))


@app.route('/campaign/delete', methods=['POST'])
@flask_login.login_required
def delete_campaign():
    camp_id = request.form['id']
    camp_ = db.session.execute(
        db.select(Campaign).filter_by(id=camp_id)).scalars().one()
    camp_.active = False
    db.session.commit()
    return redirect(url_for('campaigns'))
