import hashlib
import secrets
from datetime import datetime
from typing import Dict, Any, List

import bottle
from data_posting_service import app


def authorize():
    """ Authorize a file upload given a previously generated token. """
    config: Dict[str, Any] = app.get_app_config()

    server_token: str = config.get('secret_key')
    if not server_token:
        raise bottle.HTTPError(500, "Internal Server Error")

    if 'Authorization' not in bottle.request.headers:
        raise bottle.HTTPError(401, "No Authorization Header")

    try:
        auth_type, upload_token = bottle.request.headers.get('Authorization').split()
    except ValueError:
        raise bottle.HTTPError(401, "Incorrect Authorization Header")

    if auth_type != 'Bearer':
        raise bottle.HTTPError(401, "Incorrect Authorization Header")

    files: List[str] = [data_file.filename for data_file in bottle.request.files.getall('data_file[]')]
    token_digest: str = hash_filenames(files, server_token, get_hourly_salt())

    if not secrets.compare_digest(upload_token, token_digest):
        raise bottle.HTTPError(401, "Could not be Authorized")


def generate_token():
    """ Generate an upload specific token. """
    config: Dict[str, Any] = app.get_app_config()

    try:
        client_token: str = config['client_token']
        secret_key: str = config['secret_key']
    except KeyError:
        raise bottle.HTTPError(500, "Internal Server Error")

    auth_token: str = bottle.request.forms.get('token')

    if not secrets.compare_digest(client_token, auth_token):
        raise bottle.HTTPError(401, "Not Authorized to Generate a token")

    filenames: List[str] = bottle.request.forms.getall('filenames')
    token_digest: str = hash_filenames(filenames, secret_key, get_hourly_salt())

    return token_digest


def hash_filenames(files: List[str], pepper: str, hourly_salt: float) -> str:
    """ Use the a salt to make a sha256 hash with the filenames """
    filenames: str = ':'.join(files)
    token_string: str = f'{hourly_salt}:{pepper}:{filenames}'
    return hashlib.sha256(token_string.encode()).hexdigest()


def get_hourly_salt() -> float:
    """ Get a timestamp with the current hour """
    return datetime.utcnow().replace(minute=0, second=0, microsecond=0).timestamp()
