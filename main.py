from __future__ import print_function

import logging
from logging.handlers import RotatingFileHandler
import inspect
import os
import os.path
import pickle
import re
import traceback as tb
from typing import Callable
import json
import yaml
import subprocess

from flask import Flask, jsonify, redirect, request, render_template
from flask import url_for
from flask_cors import CORS
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from pandas import DataFrame

app = Flask(__name__, template_folder='.')
# app.wsgi_app = ReverseProxied(app.wsgi_app)
CORS(app)

app.config["creds"] = None
app.config["pickle"] = "./token.pickle"
app.config["state"] = None
app.config["redirect"] = "/"
viewIDs = []

# Items here will not be redirected to authenticate
oauth_whitelist = [
    r"^(?:\/api)?\/$",
    r"^(?:\/api)?\/oauth\/callback\/?$",
    r"^(?:\/api)?\/oauth\/authorize\/?$",
]

scopes = [
    "https://www.googleapis.com/auth/analytics.readonly"
]

discovery_urls = {
    ("analytics", "v4"): "https://analyticsreporting.googleapis.com/$discovery/rest"
}


class SetupError(Exception):
    pass


@app.before_first_request
def validate_environment():
    if "API_URL" not in os.environ:
        os.environ["API_URL"] = "https://localhost:5000"


@app.route("/")
def index():
    return render_template('gaauthentication.html')


@app.route("/auth", methods=["POST"])
def oauth_check():
    if all(re.match(path, request.path) is None for path in oauth_whitelist):
        if not app.config["creds"] or not app.config["creds"].valid:

            # Retrieve data from POST request with form's name
            client_secret = request.form['client_secret']
            client_id = request.form['client_id']
            primo_url = request.form['primo_url']
            primo_key = request.form['primo_key']

            with open("credentials.json", "r") as jsonFile:
                data = json.load(jsonFile)

            tmp = data["web"]
            data["web"]["client_secret"] = client_secret
            data["web"]["client_id"] = client_id
            with open("credentials.json", "w") as jsonFile:
                json.dump(data, jsonFile)
            # New dict from the Form for necessary configuration
            new_yaml_data_dict = {
                'primo': {
                    'host': primo_url,
                    'api_key': primo_key
                }
            }

            with open('config.yaml.secret', 'w') as yamlfile:
                yaml.safe_dump(new_yaml_data_dict, yamlfile, default_flow_style=False, allow_unicode=True,
                               encoding=None)  # Also note the safe_dump
            app.config["redirect"] = "/viewslist"
            return redirect(os.getenv("API_URL") + url_for("oauth_authorize"))


def require_access(service_name, version):
    """
    Annotate a method with the access required
    :param service_name:
    :param version:
    :return:
    """

    def receive(fn: Callable):
        def wrapper(*args, **kwargs):
            kwargs["auth"] = build(service_name, version, credentials=app.config["creds"])
            return fn(*args, **kwargs)

        wrapper.__name__ = fn.__name__
        wrapper.__signature__ = inspect.signature(fn)
        return wrapper

    return receive


@app.route("/viewslist")
@require_access("analytics", "v3")
def showListofViews(auth):
    accounts = auth.management().accounts().list().execute()

    for account in accounts["items"]:
        account_id = account["id"]
        properties = auth.management().webproperties().list(accountId=account_id).execute()
        for webp in properties["items"]:
            web_id = webp["id"]
            views = auth.management().profiles().list(
                accountId=account_id,
                webPropertyId=web_id).execute()
            for view in views["items"]:
                viewIDs.append(view['id'])
    print(viewIDs)
    return render_template('viewsList.html', viewIDs=viewIDs)


@app.route("/finalise")
def finalise():
    viewID = request.form['viewIDPicker']
    errors = []

    if not os.path.exists('/token.pickle'):
        errors.append("Cannot find token.pickle!!!")
    if viewID not in viewIDs:
        errors.append("View ID does not exist!!!")
    if not os.path.exists('/config.yaml'):
        errors.append("Cannot find config.yaml!!!")
    if errors:
        return render_template("/errors.html", errors=errors)

    path_for_config_worker = os.path.dirname(__file__) + "/configuration/token.pickle"

    os.replace(os.path.abspath('./token.pickle'), path_for_config_worker)
    new_yaml_data_dict = {
        'view_id': viewID,
        'path_to_credentials': path_for_config_worker
    }
    with open('config.yaml.secret', 'r') as yamlfile:
        cur_yaml = yaml.safe_load(yamlfile)  # Note the safe_load
        cur_yaml['analytics'].update(new_yaml_data_dict)

    if cur_yaml:
        with open('config.yaml.secret', 'w') as yamlfile:
            yaml.safe_dump(cur_yaml, yamlfile, default_flow_style=False, allow_unicode=True,
                           encoding=None)  # Also note the safe_dump
    path_for_config_worker = os.path.dirname(__file__) + "/configuration/config.yaml.secret"

    os.replace(os.path.abspath('./config.yaml.secret'), path_for_config_worker)

    return


@app.route("/oauth/callback/")
def oauth_callback():
    try:
        state = request.args["state"]
        flow = Flow.from_client_secrets_file(
            'credentials.json', scopes, state=state)
        flow.redirect_uri = os.getenv("API_URL") + url_for("oauth_callback")

        auth_url = request.url

        if auth_url.startswith("http://"):
            print("replace with https")
            auth_url = "https" + auth_url[4:]
        flow.fetch_token(authorization_response=auth_url)
        app.config["creds"] = flow.credentials
        with open(app.config["pickle"], "wb") as fin:
            pickle.dump(app.config["creds"], fin)
            app.config["state"] = None
    except:
        tb.print_exc()
    return redirect(app.config["redirect"])


@app.route("/oauth/authorize/", methods=['GET', 'POST'])
def oauth_authorize():
    if "return_to" in request.args:
        print("i'm here in app redirect")
        app.config["redirect"] = request.args["return_to"]

    # Try loading in the token from previous session
    if not app.config["creds"] and os.path.exists(app.config["pickle"]):
        with open(app.config["pickle"], "rb") as fin:
            app.config["creds"] = pickle.load(fin)

    # Check for token expiry
    if app.config["creds"] and app.config["creds"].expired and app.config["creds"].refresh_token:
        print("Refresh Token")
        app.config["creds"].refresh(Request())

    if app.config["creds"] and app.config["creds"].valid:
        print("Authorized")
        return redirect(app.config["redirect"])

    flow = Flow.from_client_secrets_file(
        'credentials.json', scopes)
    flow.redirect_uri = os.getenv("API_URL") + url_for("oauth_callback")

    auth_url, app.config["state"] = flow.authorization_url(
        prompt='consent',
        access_type='offline',
        include_granted_scopes='true'
    )
    # For some reason redirect_uri is not attached by the lib
    return redirect(auth_url)


@app.route("/oauth/deauthorize/")
def logout():
    pass


@app.route("/analytics/")
@require_access("analyticsreporting", "v4")
def access_reporting(auth):
    ids = require_analytics_id()
    data = auth.reports().batchGet(
        body={
            'reportRequests': [
                {
                    'viewId': ids,
                    'dateRanges': [{'startDate': '7daysAgo', 'endDate': 'today'}],
                    'dimensions': [{"name": "ga:pagePath"}, {"name": "ga:pageTitle"}],
                    'metrics': [{'expression': 'ga:pageviews'}, {'expression': 'ga:uniquePageviews'},
                                {'expression': 'ga:timeOnPage'}]
                }]
        }
    ).execute()
    return jsonify(data)


@app.route("/realtime/")
@require_access("analytics", "v3")
def access_realtime(auth):
    ids = require_analytics_id()
    data = auth.data().realtime().get(
        ids=f"ga:{ids}",
        metrics='rt:pageviews',
        dimensions='rt:pagePath,rt:minutesAgo,rt:country,rt:city,rt:pageTitle',
        filters="ga:pagePath=~/primo-explore/fulldisplay?/*",
        sort="rt:minutesAgo"
    ).execute()
    return jsonify(data)


@app.route("/realtime/pages/")
@require_access("analytics", "v3")
def list_realtime_urls(auth):
    ids = require_analytics_id()
    data = auth.data().realtime().get(
        ids=f"ga:{ids}",
        metrics='rt:pageviews',
        dimensions='rt:pagePath,rt:minutesAgo,rt:country,rt:city,rt:pageTitle',
        sort="rt:minutesAgo"
    ).execute()
    columns = list(header["name"] for header in data["columnHeaders"])
    df = DataFrame(data=data["rows"][3:], columns=columns)
    queries_stripped = df["rt:pagePath"].replace([r"\?.+$"], [""], regex=True)
    return jsonify({"pages": list(queries_stripped.unique())})


@app.route("/analytics/views/")
@require_access("analytics", "v3")
def show_accounts(auth):
    accounts = auth.management().accounts().list().execute()
    results = []
    for account in accounts["items"]:
        account_id = account["id"]
        properties = auth.management().webproperties().list(accountId=account_id).execute()
        for webp in properties["items"]:
            web_id = webp["id"]
            views = auth.management().profiles().list(
                accountId=account_id,
                webPropertyId=web_id).execute()
            for view in views["items"]:
                results.append(view)
    return jsonify(
        {
            "user": accounts,
            "views": results
        }
    )


def require_analytics_id():
    if "ga" not in request.args:
        raise Exception("Google analytics required")
    ga_id = request.args.get("ga")
    if not re.match("^[0-9]+$", ga_id):
        raise Exception("Invalid GA ID")
    return ga_id


if __name__ == '__main__':
    app.run(threaded=False, debug=True, ssl_context='adhoc')
